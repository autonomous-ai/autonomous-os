//! Prolonged scripted conversations with injected failures.
//!
//! One person, one supervised provider link, a speaker draining at 24,000
//! samples per second, and a scripted service that answers, stalls, closes,
//! announces closes and stays silent. The runtime clock is paused, so tens of
//! minutes of conversation run in seconds and every run with the same seed is
//! identical.
//!
//! Each request's audio carries that request's own sample value. The ledger
//! checks, for every accepted request, that only its own audio was ever
//! delivered under its name, that it ended in exactly one explicit outcome,
//! and that the service never received its input twice.
use super::*;
use crate::{BarrierPolicy, Discard, testing::FakeService};
use base64::{Engine, engine::general_purpose::STANDARD};
use std::collections::BTreeMap;

struct Random(u64);
impl Random {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    fn below(&mut self, bound: u64) -> u64 {
        self.next() % bound
    }
    fn between(&mut self, low: u64, high: u64) -> u64 {
        low + self.below(high - low + 1)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Outcome {
    /// The service completed it and every sample was delivered.
    Completed,
    /// Generation completed, the connection closed, every sample was delivered.
    Settled,
    Lost(Stage),
    /// The person moved on before it finished.
    Cancelled,
}
#[derive(Default)]
struct Record {
    /// Samples the service put on the wire for this request before any failure.
    sent: u64,
    delivered: u64,
    /// Input blocks any session of the service received for it.
    input_blocks: u64,
    outcome: Option<Outcome>,
}
#[derive(Default)]
struct Tally {
    recoveries: u64,
    assumed: u64,
    discarded: u64,
    go_away: u64,
    lost: u64,
    settled: u64,
    completed: u64,
    cancelled: u64,
    elapsed: Duration,
}

const BLOCKS: u64 = 4;
fn marker(request: u64) -> i16 {
    (request % 30_000) as i16 + 1
}
fn confirm_idle() -> Value {
    json!({"serverContent":{"turnComplete":true,"interactionStatus":"IDLE"}})
}
fn confirm_interrupted() -> Value {
    json!({"serverContent":{"interrupted":true}})
}
fn generation_complete() -> Value {
    json!({"serverContent":{"generationComplete":true}})
}

struct Conversation {
    supervisor: Supervisor<FakeConnector>,
    dials: UnboundedReceiver<Dial>,
    service: FakeService,
    handles: u64,
    ledger: BTreeMap<u64, Record>,
    tally: Tally,
    started: Instant,
    /// The request whose answer is still owed, if any.
    open: Option<u64>,
    /// An activity the service has open with no End sent.
    activity_open: bool,
    /// The request this session of the service has been asked to answer and
    /// has not completed. Only that can be interrupted, and only on the
    /// session that received it: a new session knows nothing of it.
    service_owes: Option<u64>,
    /// An outage was announced and no connection has been ready since.
    link_down: bool,
    next_request: u64,
}

impl Conversation {
    async fn begin(policy: RecoveryPolicy) -> Self {
        let config = config().barrier_policy(BarrierPolicy::AssumeAfterQuiet);
        let (dialer, mut dials) = connector(config);
        let mut supervisor = Supervisor::new(dialer, policy).unwrap();
        let serve = async {
            let mut dial = next_dial(&mut dials).await;
            dial.service.accept().await;
            dial.service
        };
        let (first, service) = tokio::join!(timeout(WAIT, supervisor.next()), serve);
        assert!(matches!(first, Ok(Ok(Notice::Ready { .. }))));
        let mut conversation = Self {
            supervisor,
            dials,
            service,
            handles: 0,
            ledger: BTreeMap::new(),
            tally: Tally::default(),
            started: Instant::now(),
            open: None,
            activity_open: false,
            service_owes: None,
            link_down: false,
            next_request: 0,
        };
        conversation.issue_handle().await;
        conversation
    }
    async fn issue_handle(&mut self) {
        self.handles += 1;
        self.service
            .send(handle(&format!("HANDLE-{}", self.handles)))
            .await;
    }

    /// One client message, or `None` when the service hears nothing in time.
    async fn heard(&mut self, wait: Duration) -> Option<&'static str> {
        let message = self.service.try_receive(wait).await?;
        let input = &message["realtimeInput"];
        if input.get("activityStart").is_some() {
            self.activity_open = true;
            return Some("start");
        }
        if input.get("activityEnd").is_some() {
            self.activity_open = false;
            return Some("end");
        }
        let pcm = STANDARD
            .decode(input["audio"]["data"].as_str().expect("an input block"))
            .unwrap();
        let value = i16::from_le_bytes([pcm[0], pcm[1]]);
        // Input blocks carry their request in the same way answers do.
        let request = self
            .ledger
            .iter()
            .find(|(request, _)| marker(**request) == value)
            .map(|(request, _)| *request)
            .expect("input of a known request");
        self.ledger.get_mut(&request).unwrap().input_blocks += 1;
        Some("block")
    }

    /// The person speaks a new request to the end. Whatever was still owed is
    /// cancelled first, exactly as the provider worker does on a new owner.
    /// `confirm` is how the service reacts to being interrupted, if it is.
    async fn say(&mut self, confirm: Confirm) -> u64 {
        self.next_request += 1;
        let request = self.next_request;
        self.ledger.insert(request, Record::default());
        let interrupting = self.open.take();
        if let Some(old) = interrupting {
            self.supervisor.retire(RequestId::new(old).unwrap());
            let record = self.ledger.get_mut(&old).unwrap();
            if record.outcome.is_none() {
                record.outcome = Some(Outcome::Cancelled);
                self.tally.cancelled += 1;
            }
        }
        let id = RequestId::new(request).unwrap();
        self.supervisor.try_start(id).unwrap();
        for sequence in 0..BLOCKS {
            self.supervisor
                .try_audio(
                    id,
                    sequence,
                    std::time::Instant::now(),
                    &[marker(request); 160],
                )
                .unwrap();
        }
        self.supervisor.try_end(id).unwrap();
        // The opening words arrive whole and in order before any confirmation.
        if !self.activity_open {
            assert_eq!(self.heard(WAIT).await, Some("start"), "request {request}");
        }
        for _ in 0..BLOCKS {
            assert_eq!(self.heard(WAIT).await, Some("block"), "request {request}");
        }
        if self.service_owes.take().is_some() {
            match confirm {
                Confirm::Both => {
                    self.service.send(confirm_interrupted()).await;
                    self.service.send(confirm_idle()).await;
                }
                Confirm::Completion => self.service.send(confirm_idle()).await,
                // Nothing: the transport must assume, and say so.
                Confirm::Silent => {}
            }
        }
        // Committed once the earlier response has settled, by word or by bound.
        assert_eq!(
            self.heard(Duration::from_secs(5)).await,
            Some("end"),
            "request {request} was never committed"
        );
        self.open = Some(request);
        self.service_owes = Some(request);
        request
    }
    /// The service finishes the answer it owes.
    async fn complete(&mut self) {
        self.service.send(generation_complete()).await;
        self.service.send(confirm_idle()).await;
        self.service_owes = None;
    }

    /// The service writes `seconds` of the answer to `request` in `chunk`
    /// sample messages. Everything is on the wire at once: generation outruns
    /// playback, and the transport holds it.
    async fn answer(&mut self, request: u64, seconds: u64, chunk: usize) {
        let mut left = seconds * RATE;
        while left > 0 {
            let samples = (chunk as u64).min(left) as usize;
            self.service.send(audio(marker(request), samples)).await;
            left -= samples as u64;
        }
        self.ledger.get_mut(&request).unwrap().sent += seconds * RATE;
    }

    /// Accept the supervisor's next connection attempt as a new session.
    async fn reconnect(&mut self) {
        let deadline = Instant::now() + Duration::from_secs(30);
        let dial = loop {
            self.supervisor.maintain();
            if let Ok(dial) = self.dials.try_recv() {
                break dial;
            }
            assert!(Instant::now() < deadline, "no reconnect attempt");
            tokio::time::sleep(Duration::from_millis(2)).await;
        };
        assert!(dial.resumed, "context is presented on every reconnect");
        self.service = dial.service;
        self.activity_open = false;
        self.service_owes = None;
        let setup = self.service.receive().await;
        assert_eq!(
            setup["setup"]["sessionResumption"]["handle"],
            format!("HANDLE-{}", self.handles)
        );
        self.service.send(json!({"setupComplete": {}})).await;
        self.issue_handle().await;
        self.tally.recoveries += 1;
    }

    /// Play at speaking speed until `stop` holds, keeping the ledger. Pings
    /// are answered while the speaker is busy, as a live service would.
    async fn play(&mut self, stop: Stop) {
        let mut heard = 0u64;
        loop {
            let ready = !self.link_down && self.supervisor.is_connected();
            let done = match stop {
                Stop::AtOutcome => self.open.is_none() && ready,
                Stop::AfterSeconds(seconds) => heard >= seconds * RATE,
                Stop::Recoveries(count) => self.tally.recoveries >= count && ready,
            };
            if done {
                return;
            }
            self.supervisor.maintain();
            let notice = match timeout(Duration::from_millis(20), self.supervisor.next()).await {
                Ok(notice) => notice.expect("no terminal failure in a recoverable scenario"),
                Err(_) => {
                    // Nothing to deliver right now. Time still passes.
                    if let Some(message) = self.service.rest(Duration::from_millis(20)).await {
                        panic!("unexpected client message during playback: {message}");
                    }
                    continue;
                }
            };
            match notice {
                Notice::Event(Event::Audio { lineage, pcm, .. }) => {
                    let request = lineage.request.get();
                    assert!(
                        pcm.iter().all(|sample| *sample == marker(request)),
                        "audio delivered under request {request} belongs to another request"
                    );
                    let record = self.ledger.get_mut(&request).unwrap();
                    assert!(record.outcome.is_none(), "audio after {:?}", record.outcome);
                    record.delivered += pcm.len() as u64;
                    assert!(record.delivered <= record.sent);
                    heard += pcm.len() as u64;
                    let time = Duration::from_micros(pcm.len() as u64 * 1_000_000 / RATE);
                    if let Some(message) = self.service.rest(time).await {
                        panic!("unexpected client message during playback: {message}");
                    }
                }
                Notice::Event(Event::TurnComplete {
                    lineage,
                    idle: true,
                }) => {
                    let request = lineage.request.get();
                    let record = self.ledger.get_mut(&request).unwrap();
                    if record.outcome.is_none() {
                        assert_eq!(record.delivered, record.sent, "request {request}");
                        record.outcome = Some(Outcome::Completed);
                        self.tally.completed += 1;
                        self.open = None;
                    }
                }
                Notice::Settled { lineage, .. } => {
                    let request = lineage.request.get();
                    let record = self.ledger.get_mut(&request).unwrap();
                    assert!(record.outcome.is_none());
                    assert_eq!(record.delivered, record.sent, "request {request}");
                    record.outcome = Some(Outcome::Settled);
                    self.tally.settled += 1;
                    self.open = None;
                }
                Notice::TurnLost { lineage, stage, .. } => {
                    let request = lineage.request.get();
                    let record = self.ledger.get_mut(&request).unwrap();
                    assert!(record.outcome.is_none());
                    // Everything that arrived before the loss was delivered.
                    assert_eq!(record.delivered, record.sent, "request {request}");
                    record.outcome = Some(Outcome::Lost(stage));
                    self.tally.lost += 1;
                    self.open = None;
                }
                Notice::Unavailable { .. } => {
                    self.link_down = true;
                    self.reconnect().await;
                }
                Notice::Ready { after, .. } => {
                    assert!(after.is_some());
                    self.link_down = false;
                }
                Notice::Event(Event::BarrierAssumed { .. }) => self.tally.assumed += 1,
                Notice::Event(Event::Discarded { reason, .. }) => {
                    assert_ne!(
                        reason,
                        Discard::LateOutput,
                        "no scenario here sends late audio"
                    );
                    self.tally.discarded += 1;
                }
                Notice::Event(Event::GoAway { .. }) => self.tally.go_away += 1,
                Notice::Event(_) => {}
            }
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Confirm {
    Both,
    Completion,
    Silent,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Stop {
    /// The open request reached an outcome and a connection is ready.
    AtOutcome,
    AfterSeconds(u64),
    /// At least this many recoveries have happened and a connection is ready.
    Recoveries(u64),
}

async fn converse(turns: u64, seed: u64) -> Tally {
    let policy = RecoveryPolicy {
        // Failures are injected far more often than any real link would fail.
        max_recoveries: 64,
        window: Duration::from_secs(60),
        ..RecoveryPolicy::default()
    };
    let mut random = Random(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1);
    let mut talk = Conversation::begin(policy).await;
    for _ in 0..turns {
        // The person speaks only when a connection is ready. Speaking into an
        // outage is the provider worker's case, covered by its own tests.
        talk.play(Stop::Recoveries(0)).await;
        let confirm = match random.below(3) {
            0 => Confirm::Both,
            1 => Confirm::Completion,
            _ => Confirm::Silent,
        };
        let request = talk.say(confirm).await;
        let seconds = match random.below(100) {
            0..=79 => random.between(1, 4),
            80..=94 => random.between(10, 30),
            _ => random.between(60, 180),
        };
        let chunk = [960, 4_800, 24_000, LARGEST][random.below(4) as usize];
        match random.below(100) {
            // A complete answer, played to the end.
            0..=44 => {
                talk.answer(request, seconds, chunk).await;
                talk.complete().await;
                talk.play(Stop::AtOutcome).await;
            }
            // The person interrupts part-way; the next turn cancels it.
            45..=59 => {
                talk.answer(request, seconds, chunk).await;
                talk.play(Stop::AfterSeconds(random.between(1, seconds)))
                    .await;
            }
            // The person continues before any answer: nothing is sent at all.
            60..=69 => {}
            // The connection dies before generation completes.
            70..=77 => {
                talk.answer(request, seconds, chunk).await;
                talk.service.close(1011, "internal").await;
                talk.play(Stop::AtOutcome).await;
                assert_eq!(
                    talk.ledger[&request].outcome,
                    Some(Outcome::Lost(Stage::Responding))
                );
            }
            // The connection dies with the whole answer already received.
            78..=85 => {
                talk.answer(request, seconds, chunk).await;
                talk.service.send(generation_complete()).await;
                talk.service.close(1008, "The operation was aborted").await;
                talk.play(Stop::AtOutcome).await;
                assert_eq!(talk.ledger[&request].outcome, Some(Outcome::Settled));
            }
            // The answer completes, then the idle session is closed.
            86..=90 => {
                talk.answer(request, seconds, chunk).await;
                talk.complete().await;
                talk.play(Stop::AtOutcome).await;
                let recoveries = talk.tally.recoveries;
                talk.service.close(1008, "The operation was aborted").await;
                talk.play(Stop::Recoveries(recoveries + 1)).await;
            }
            // The service goes quiet mid-answer without closing.
            91..=94 => {
                talk.answer(request, seconds.min(20), chunk).await;
                talk.play(Stop::AtOutcome).await;
                assert_eq!(
                    talk.ledger[&request].outcome,
                    Some(Outcome::Lost(Stage::Responding))
                );
            }
            // The question gets no answer at all.
            95..=96 => {
                talk.play(Stop::AtOutcome).await;
                assert_eq!(
                    talk.ledger[&request].outcome,
                    Some(Outcome::Lost(Stage::AwaitingResponse))
                );
            }
            // The service announces a close mid-answer, then finishes it.
            _ => {
                let recoveries = talk.tally.recoveries;
                talk.answer(request, seconds, chunk).await;
                talk.service
                    .send(json!({"goAway":{"timeLeft":"10s"}}))
                    .await;
                talk.complete().await;
                talk.play(Stop::AtOutcome).await;
                // The announced close is taken at the clean boundary.
                talk.play(Stop::Recoveries(recoveries + 1)).await;
            }
        }
    }
    // The last request may still be open; the ledger must be whole otherwise.
    let open = talk.open;
    for (request, record) in &talk.ledger {
        if Some(*request) == open {
            continue;
        }
        assert!(record.outcome.is_some(), "request {request} has no outcome");
        // Its input reached the service exactly once across every session.
        assert_eq!(record.input_blocks, BLOCKS, "request {request}");
    }
    let mut tally = talk.tally;
    tally.elapsed = talk.started.elapsed();
    tally
}

fn report(name: &str, turns: u64, seed: u64, tally: &Tally) {
    println!(
        "MEASURED {name} turns={turns} seed={seed} virtual_minutes={:.1} completed={} cancelled={} settled={} lost={} recoveries={} assumed_barriers={} go_away={} dropped_events={}",
        tally.elapsed.as_secs_f64() / 60.0,
        tally.completed,
        tally.cancelled,
        tally.settled,
        tally.lost,
        tally.recoveries,
        tally.assumed,
        tally.go_away,
        tally.discarded
    );
}

#[tokio::test(start_paused = true)]
async fn sixty_turn_conversation_with_injected_failures_keeps_every_request_accounted_for() {
    let tally = converse(60, 1).await;
    report("scripted_conversation", 60, 1, &tally);
    assert!(tally.recoveries > 0 && tally.lost > 0 && tally.settled > 0 && tally.cancelled > 0);
}

#[tokio::test(start_paused = true)]
#[ignore = "several hundred turns per seed; run explicitly"]
async fn prolonged_conversations_with_injected_failures_keep_every_request_accounted_for() {
    for seed in [2, 3, 5, 8] {
        let tally = converse(400, seed).await;
        report("prolonged_scripted_conversation", 400, seed, &tally);
    }
}
