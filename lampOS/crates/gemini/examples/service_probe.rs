//! Narrow probes of the live service for behavior this transport depends on
//! and that cannot be established offline. Each probe makes a few short
//! requests with one cached spoken question and prints one JSON line of event
//! kinds and timings. It prints no transcript, audio, handle or credential,
//! and it opens no microphone or speaker.
//!
//! ```sh
//! cargo run --locked -p lamp-gemini --example service_probe -- \
//!     /absolute/private/provider.json /path/to/question.wav interrupt-before-output
//! ```
//!
//! The provider file has the fields the runtime uses: `endpoint`, `model`,
//! `voice`, `credential_file`, and optional `authentication`, `thinking_level`
//! and `language_code`. Probes: `interrupt-before-output`, `handles`,
//! `handles-requested`, `resume`, `idle-close`. `fixture` is offline: it only
//! reports what would be spoken from the WAV file.
use lamp_gemini::{
    BarrierPolicy, Connection, Credential, Event, RequestId, Resumption, SessionConfig, SessionId,
    State, ThinkingLevel, Timeouts,
};
use serde::Deserialize;
use std::{
    error::Error,
    fs,
    os::unix::fs::PermissionsExt,
    path::Path,
    time::{Duration, Instant},
};

type Outcome<T> = Result<T, Box<dyn Error + Send + Sync>>;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Provider {
    endpoint: String,
    model: String,
    voice: String,
    credential_file: String,
    #[serde(default)]
    authentication: Option<String>,
    #[serde(default)]
    thinking_level: Option<ThinkingLevel>,
    #[serde(default)]
    language_code: Option<String>,
}

/// Regular file, readable by its owner only, of bounded size.
fn private(path: &Path, max: u64) -> Outcome<String> {
    let info = fs::symlink_metadata(path)?;
    if !info.is_file() || info.permissions().mode() & 0o077 != 0 || info.len() > max {
        return Err("configuration must be a bounded regular file with mode 0600".into());
    }
    Ok(fs::read_to_string(path)?)
}

fn session(path: &Path) -> Outcome<SessionConfig> {
    let provider: Provider = serde_json::from_str(&private(path, 16_384)?)
        .map_err(|_| "invalid private provider configuration")?;
    let secret = private(Path::new(&provider.credential_file), 4_098)?;
    let secret = secret.trim_end_matches(['\r', '\n']);
    let credential = match provider.authentication.as_deref() {
        None | Some("api_key") => Credential::api_key(secret)?,
        Some("bearer") => Credential::bearer(secret)?,
        Some("ephemeral") => Credential::ephemeral(secret)?,
        Some(_) => return Err("unknown authentication".into()),
    };
    let mut config = SessionConfig::new(&provider.endpoint, credential)?
        .model(&provider.model)?
        .voice(&provider.voice)?
        .instruction("You are a voice assistant. Answer every question in one short sentence.")?;
    if let Some(level) = provider.thinking_level {
        config = config.thinking_level(level)?;
    }
    if let Some(language) = &provider.language_code {
        config = config.language_code(language)?;
    }
    Ok(config)
}

/// 16 kHz mono from a 16-bit PCM WAV, by linear interpolation. Adequate for a
/// spoken fixture; not a production decoder or resampler.
fn speech(path: &Path) -> Outcome<Vec<i16>> {
    let bytes = fs::read(path)?;
    if bytes.len() < 12 || &bytes[..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return Err("speech fixture must be a RIFF WAVE file".into());
    }
    let word = |at: usize| u16::from_le_bytes([bytes[at], bytes[at + 1]]);
    let long =
        |at: usize| u32::from_le_bytes([bytes[at], bytes[at + 1], bytes[at + 2], bytes[at + 3]]);
    let (mut format, mut data, mut at) = (None, None, 12);
    while at + 8 <= bytes.len() {
        let size = long(at + 4) as usize;
        let body = at + 8;
        let end = body.saturating_add(size).min(bytes.len());
        match &bytes[at..at + 4] {
            b"fmt " if size >= 16 && end - body >= 16 => {
                format = Some((word(body), word(body + 2), long(body + 4), word(body + 14)));
            }
            b"data" => data = Some(&bytes[body..end]),
            _ => {}
        }
        at = end + (size & 1);
    }
    let (Some((kind, channels, rate, bits)), Some(data)) = (format, data) else {
        return Err("speech fixture has no format or data chunk".into());
    };
    // 1 is integer PCM; 0xFFFE is the extensible form of the same.
    if !matches!(kind, 1 | 0xFFFE) || bits != 16 || channels == 0 || rate == 0 {
        return Err("speech fixture must be 16-bit integer PCM".into());
    }
    let channels = usize::from(channels);
    let mono: Vec<f32> = data
        .chunks_exact(2 * channels)
        .map(|frame| {
            frame
                .chunks_exact(2)
                .map(|pair| f32::from(i16::from_le_bytes([pair[0], pair[1]])))
                .sum::<f32>()
                / channels as f32
        })
        .collect();
    if mono.is_empty() {
        return Err("speech fixture holds no audio".into());
    }
    let step = rate as f32 / 16_000.0;
    let length = (mono.len() as f32 / step) as usize;
    Ok((0..length)
        .map(|index| {
            let position = index as f32 * step;
            let (low, fraction) = (position as usize, position.fract());
            let low = low.min(mono.len() - 1);
            let next = mono.get(low + 1).copied().unwrap_or(mono[low]);
            (mono[low] * (1.0 - fraction) + next * fraction) as i16
        })
        .collect())
}

/// Drop leading and trailing silence, keeping 200 ms around the speech, so a
/// padded fixture does not hold the activity open for many silent seconds.
fn trim(speech: &[i16]) -> &[i16] {
    let loud = |sample: &i16| sample.unsigned_abs() > 328; // about -40 dBFS
    let (Some(first), Some(last)) = (speech.iter().position(loud), speech.iter().rposition(loud))
    else {
        return speech;
    };
    &speech[first.saturating_sub(3_200)..(last + 3_200).min(speech.len())]
}

/// Speak the fixture in 10 ms blocks at real time, then optionally end input.
async fn say(connection: &Connection, id: u64, speech: &[i16], end: bool) -> Outcome<()> {
    let request = RequestId::new(id)?;
    let input = connection.input();
    input.try_start(request)?;
    for (sequence, block) in speech.chunks(160).enumerate() {
        input.try_audio(request, sequence as u64, Instant::now(), block)?;
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    if end {
        input.try_end(request)?;
    }
    Ok(())
}

/// Event kinds for one request with milliseconds since `since`, until the
/// request completes, the connection ends or `wait` passes.
async fn observe(
    connection: &mut Connection,
    since: Instant,
    until_complete: u64,
    wait: Duration,
) -> Vec<serde_json::Value> {
    let mut seen = Vec::new();
    let mut note = |kind: &str, request: Option<u64>| {
        seen.push(serde_json::json!({"kind":kind,"request":request,"ms":since.elapsed().as_millis() as u64}));
    };
    let deadline = tokio::time::Instant::now() + wait;
    let mut first_audio = std::collections::BTreeSet::new();
    loop {
        let Ok(event) = tokio::time::timeout_at(deadline, connection.next_event()).await else {
            note("observation_window_ended", None);
            break;
        };
        match event {
            None => {
                note(&format!("connection_ended:{:?}", connection.state()), None);
                break;
            }
            Some(Event::Audio { lineage, .. }) => {
                if first_audio.insert(lineage.request.get()) {
                    note("first_audio", Some(lineage.request.get()));
                }
            }
            Some(Event::Interrupted { lineage }) => {
                note("interrupted", Some(lineage.request.get()))
            }
            Some(Event::GenerationComplete { lineage }) => {
                note("generation_complete", Some(lineage.request.get()));
            }
            Some(Event::TurnComplete { lineage, idle }) => {
                let request = lineage.request.get();
                note(
                    if idle {
                        "turn_complete_idle"
                    } else {
                        "turn_complete_in_progress"
                    },
                    Some(request),
                );
                if idle && request == until_complete {
                    break;
                }
            }
            Some(Event::BarrierAssumed {
                superseded,
                successor,
                ..
            }) => {
                note(
                    "no_confirmation_within_barrier",
                    superseded.or(successor).map(RequestId::get),
                );
            }
            Some(Event::Discarded { reason, .. }) => note(&format!("dropped:{reason:?}"), None),
            Some(Event::GoAway { .. }) => note("go_away", None),
            Some(_) => {}
        }
    }
    seen
}

async fn probe(config: SessionConfig, speech: &[i16], name: &str) -> Outcome<serde_json::Value> {
    let timeouts = Timeouts {
        // Observe for as long as the transport allows before assuming.
        barrier: Duration::from_secs(5),
        ..Timeouts::default()
    };
    let config = config
        .timeouts(timeouts)?
        .barrier_policy(BarrierPolicy::AssumeAfterQuiet);
    let started = Instant::now();
    Ok(match name {
        // Does the service confirm an interruption of a request it has not
        // begun to answer? This is the pause-and-resume case.
        "interrupt-before-output" => {
            let mut connection =
                lamp_gemini::connect(config.resumption(Resumption::Retain), SessionId::new(1)?)
                    .await?;
            say(&connection, 1, speech, true).await?;
            let resumed = Instant::now();
            say(&connection, 2, speech, true).await?;
            let events = observe(&mut connection, resumed, 2, Duration::from_secs(40)).await;
            serde_json::json!({"probe":name,"since":"second activityStart","events":events})
        }
        // Are resumption handles issued, unasked or when asked?
        "handles" | "handles-requested" => {
            let mode = if name == "handles" {
                Resumption::Retain
            } else {
                Resumption::Request
            };
            let mut connection =
                lamp_gemini::connect(config.resumption(mode), SessionId::new(1)?).await?;
            let after_setup = connection.resumption().is_some();
            say(&connection, 1, speech, true).await?;
            let events = observe(&mut connection, started, 1, Duration::from_secs(40)).await;
            tokio::time::sleep(Duration::from_secs(2)).await;
            let point = connection.resumption();
            serde_json::json!({"probe":name,"handle_after_setup":after_setup,
                "handle_after_one_answer":point.is_some(),
                "handle_current":point.as_ref().is_some_and(|point| point.is_current()),
                "events":events})
        }
        // Is a handle accepted on a new connection, and does that session answer?
        "resume" => {
            let mut first = lamp_gemini::connect(
                config.clone().resumption(Resumption::Request),
                SessionId::new(1)?,
            )
            .await?;
            say(&first, 1, speech, true).await?;
            observe(&mut first, started, 1, Duration::from_secs(40)).await;
            tokio::time::sleep(Duration::from_secs(2)).await;
            let Some(point) = first.resumption() else {
                return Ok(serde_json::json!({"probe":name,"handle_issued":false}));
            };
            drop(first);
            let resumed = Instant::now();
            let second = lamp_gemini::connect(
                config
                    .resumption(Resumption::Request)
                    .resume(Some(point.handle().clone())),
                SessionId::new(2)?,
            )
            .await;
            let mut second = match second {
                Ok(connection) => connection,
                Err(error) => {
                    return Ok(serde_json::json!({"probe":name,"handle_issued":true,
                        "resumed_setup":format!("{error:?}")}));
                }
            };
            say(&second, 1, speech, true).await?;
            let events = observe(&mut second, resumed, 1, Duration::from_secs(40)).await;
            serde_json::json!({"probe":name,"handle_issued":true,"resumed_setup":"accepted",
                "resumed_setup_ms":resumed.elapsed().as_millis() as u64,"events":events})
        }
        // How long does an unused session live, and how does it end?
        "idle-close" => {
            let mut connection =
                lamp_gemini::connect(config.resumption(Resumption::Retain), SessionId::new(1)?)
                    .await?;
            let events = observe(&mut connection, started, 0, Duration::from_secs(300)).await;
            serde_json::json!({"probe":name,"final_state":format!("{:?}", connection.state()),
                "ready":connection.state() == State::Ready,"events":events})
        }
        _ => return Err("unknown probe".into()),
    })
}

fn main() -> Outcome<()> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(run())
}

async fn run() -> Outcome<()> {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    let [provider, wav, name] = arguments.as_slice() else {
        return Err("usage: service_probe PRIVATE_PROVIDER.json QUESTION.wav PROBE".into());
    };
    let decoded = speech(Path::new(wav))?;
    let speech = trim(&decoded);
    if speech.len() < 8_000 || speech.len() > 16_000 * 30 {
        return Err("speech fixture must hold between 0.5 and 30 seconds of speech".into());
    }
    if name == "fixture" {
        // Offline: what would be spoken. No provider file is read.
        println!(
            "{}",
            serde_json::json!({"probe":name,"file_seconds":decoded.len() as f64 / 16_000.0,
                "spoken_seconds":speech.len() as f64 / 16_000.0,"blocks":speech.len().div_ceil(160)})
        );
        return Ok(());
    }
    let result = probe(session(Path::new(provider))?, speech, name).await?;
    println!("{result}");
    Ok(())
}
