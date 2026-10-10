//! Answers at speaking speed. The consumer drains exactly 24,000 samples per
//! second, as a speaker does; the scripted service generates faster, stalls,
//! bursts and withholds its idle completion until it assumes playback is over.
//! The runtime clock is paused, so these cover minutes in well under a second
//! and every timing below is exact, not load-dependent.
use super::*;
use tokio::time::{Instant as Clock, sleep};

const RATE: u64 = OUTPUT_RATE as u64;
/// Largest audio message the codec accepts: two seconds.
const LARGEST: usize = crate::MAX_OUTPUT_SAMPLES;

fn sample(index: u64) -> i16 {
    (index % 30_000) as i16
}
/// One audio message continuing a session-long ramp at `start`.
fn ramp(start: u64, samples: usize) -> Value {
    let bytes: Vec<u8> = (start..start + samples as u64)
        .flat_map(|index| sample(index).to_le_bytes())
        .collect();
    json!({"serverContent":{"modelTurn":{"parts":[{"inlineData":{
        "mimeType":"audio/pcm;rate=24000","data":STANDARD.encode(bytes)}}]}}})
}
fn playing_time(samples: u64) -> Duration {
    Duration::from_micros(samples * 1_000_000 / RATE)
}

/// Let scripted time pass while still answering keepalive pings, as a real
/// service does throughout a long answer. Client messages are not expected.
async fn rest(server: &mut MockSocket, until: Clock) {
    loop {
        tokio::select! {
            _ = tokio::time::sleep_until(until) => return,
            message = server.next() => match message {
                Some(Ok(Message::Ping(_) | Message::Pong(_))) => server.flush().await.unwrap(),
                Some(Ok(other)) => panic!("unexpected client message while speaking: {other:?}"),
                // The client ended the connection; the scenario decides why.
                Some(Err(_)) | None => return std::future::pending().await,
            },
        }
    }
}
/// The connection's failure, however long the scenario takes to reach it.
async fn failure(connection: &Connection) -> Error {
    let mut state = connection.subscribe_state();
    let failed = state.wait_for(|state| matches!(state, State::Disconnected(_)));
    match *timeout(Duration::from_secs(900), failed)
        .await
        .unwrap()
        .unwrap()
    {
        State::Disconnected(error) => error,
        other => panic!("not a failure: {other:?}"),
    }
}

struct Answer {
    seconds: u64,
    chunk: usize,
    /// Generation speed relative to real time.
    speed: u32,
    /// After this many seconds of audio, the network delivers nothing for the
    /// given time, then releases everything generated meanwhile at once.
    stall: Option<(u64, Duration)>,
    /// Send the idle completion once playback is assumed over. Without it the
    /// script returns as soon as generation completes.
    settle: bool,
}
struct Spoken {
    /// Time from the first audio message to the last one being written.
    written_in: Duration,
    server: MockSocket,
}
/// The service side of one answer.
async fn speak(mut server: MockSocket, answer: Answer) -> Spoken {
    let total = answer.seconds * RATE;
    let started = Clock::now();
    let mut sent = 0u64;
    let mut stall = answer.stall;
    while sent < total {
        let samples = (answer.chunk as u64).min(total - sent) as usize;
        send_json(&mut server, ramp(sent, samples)).await;
        sent += samples as u64;
        let mut wait = playing_time(samples as u64) / answer.speed;
        if let Some((at, silence)) = stall
            && sent >= at * RATE
        {
            stall = None;
            rest(&mut server, Clock::now() + silence).await;
            // Generation went on during the stall: that audio arrives at once.
            let backlog = (silence.as_micros() as u64 * RATE / 1_000_000 * u64::from(answer.speed))
                .min(total - sent);
            let mut released = 0;
            while released < backlog {
                let samples = (answer.chunk as u64).min(backlog - released) as usize;
                server
                    .feed(Message::text(ramp(sent, samples).to_string()))
                    .await
                    .unwrap();
                sent += samples as u64;
                released += samples as u64;
            }
            server.flush().await.unwrap();
            wait = Duration::ZERO;
        }
        rest(&mut server, Clock::now() + wait).await;
    }
    let written_in = started.elapsed();
    send_json(&mut server, generated()).await;
    if answer.settle {
        // The service withholds the idle completion while it assumes playback.
        rest(&mut server, started + playing_time(total)).await;
        send_json(&mut server, idle()).await;
    }
    Spoken { written_in, server }
}

struct Played {
    samples: u64,
    took: Duration,
}
/// Drain one answer at 24,000 samples per second, checking every sample.
/// `pause` stops playback once, after the given number of samples.
async fn play(
    connection: &mut Connection,
    request: RequestId,
    mut pause: Option<(u64, Duration)>,
) -> Played {
    let mut samples = 0u64;
    let mut started = None;
    let mut generation = false;
    loop {
        let Some(event) = timeout(Duration::from_secs(900), connection.next_event())
            .await
            .expect("an answer event")
        else {
            panic!(
                "answer cut short after {samples} samples: {:?}",
                connection.state()
            );
        };
        match event {
            Event::Audio { lineage, pcm, .. } => {
                assert_eq!(lineage.request, request);
                started.get_or_insert_with(Clock::now);
                for (offset, value) in pcm.iter().enumerate() {
                    assert_eq!(*value, sample(samples + offset as u64), "sample order");
                }
                samples += pcm.len() as u64;
                sleep(playing_time(pcm.len() as u64)).await;
                if let Some((after, silence)) = pause
                    && samples >= after
                {
                    pause = None;
                    sleep(silence).await;
                }
            }
            Event::GenerationComplete { lineage } => {
                assert_eq!(lineage.request, request);
                generation = true;
            }
            Event::TurnComplete { lineage, idle } => {
                assert_eq!((lineage.request, idle), (request, true));
                assert!(generation, "completion follows generation");
                return Played {
                    samples,
                    took: started.expect("some audio").elapsed(),
                };
            }
            other => panic!("unexpected event at speaking speed: {other:?}"),
        }
    }
}

/// Run one answer end to end with default timeouts unless given.
async fn converse(
    configuration: SessionConfig,
    answer: Answer,
    pause: Option<(u64, Duration)>,
) -> (Played, Spoken, Connection) {
    let (mut connection, mut server) = mock_with(configuration).await;
    let request = ended_turn(&mut connection, &mut server, 1).await;
    let service = tokio::spawn(speak(server, answer));
    let played = play(&mut connection, request, pause).await;
    let spoken = service.await.unwrap();
    assert_eq!(connection.state(), State::Ready);
    (played, spoken, connection)
}

#[tokio::test(start_paused = true)]
async fn sixty_second_answer_in_largest_chunks_plays_out_at_speaking_speed() {
    // A speaker takes one two-second message every two seconds. Nothing in
    // the default bounds may mistake that for a consumer that stopped.
    let answer = Answer {
        seconds: 60,
        chunk: LARGEST,
        speed: 10,
        stall: None,
        settle: true,
    };
    let (played, spoken, _connection) =
        converse(configuration(Timeouts::default()), answer, None).await;
    assert_eq!(played.samples, 60 * RATE);
    assert_eq!(played.took, Duration::from_secs(60));
    // Received at network speed, whatever the playback speed.
    assert!(
        spoken.written_in <= Duration::from_secs(7),
        "{:?}",
        spoken.written_in
    );
}

#[tokio::test(start_paused = true)]
async fn two_minute_answer_in_small_chunks_plays_out_at_speaking_speed() {
    let answer = Answer {
        seconds: 120,
        chunk: 2_400,
        speed: 3,
        stall: None,
        settle: true,
    };
    let (played, spoken, _connection) =
        converse(configuration(Timeouts::default()), answer, None).await;
    assert_eq!(played.samples, 120 * RATE);
    assert_eq!(played.took, Duration::from_secs(120));
    assert!(
        spoken.written_in <= Duration::from_secs(41),
        "{:?}",
        spoken.written_in
    );
}

#[tokio::test(start_paused = true)]
async fn three_minute_answer_survives_a_network_stall_and_the_burst_after_it() {
    // Nine silent seconds, just inside the stall bound, then everything
    // generated meanwhile at once: 36 s of audio in a single flush.
    let answer = Answer {
        seconds: 180,
        chunk: LARGEST,
        speed: 4,
        stall: Some((30, Duration::from_secs(9))),
        settle: true,
    };
    let (played, spoken, _connection) =
        converse(configuration(Timeouts::default()), answer, None).await;
    assert_eq!(played.samples, 180 * RATE);
    // Thirty seconds were buffered before the stall, so playback never waited.
    assert_eq!(played.took, Duration::from_secs(180));
    assert!(
        spoken.written_in <= Duration::from_secs(55),
        "{:?}",
        spoken.written_in
    );
}

#[tokio::test(start_paused = true)]
async fn playback_paused_inside_the_delivery_bound_is_not_a_failure() {
    // The speaker buffer is full for 25 s: nothing is taken, output waits.
    let answer = Answer {
        seconds: 90,
        chunk: LARGEST,
        speed: 10,
        stall: None,
        settle: true,
    };
    let pause = Some((10 * RATE, Duration::from_secs(25)));
    let (played, _spoken, _connection) =
        converse(configuration(Timeouts::default()), answer, pause).await;
    assert_eq!(played.samples, 90 * RATE);
    assert_eq!(played.took, Duration::from_secs(115));
}

#[tokio::test(start_paused = true)]
async fn consumer_that_stops_taking_output_still_fails_at_the_delivery_bound() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let request = ended_turn(&mut connection, &mut server, 1).await;
    let service = tokio::spawn(speak(
        server,
        Answer {
            seconds: 90,
            chunk: LARGEST,
            speed: 10,
            stall: None,
            settle: true,
        },
    ));
    assert!(
        matches!(event(&mut connection).await, Event::Audio { lineage, .. } if lineage.request == request)
    );
    let stopped = Clock::now();
    // Longer than any event's playing time plus the tolerated pause.
    assert_eq!(failure(&connection).await, Error::Backpressure);
    // Measured from when output first waited with nothing taken: the 16-event
    // queue took a few seconds to fill after the consumer stopped.
    let bound = Timeouts::default().deliver;
    assert!(stopped.elapsed() >= bound && stopped.elapsed() < bound + Duration::from_secs(10));
    service.abort();
}

#[tokio::test(start_paused = true)]
async fn delivery_bound_shorter_than_one_message_of_audio_fails_at_speaking_speed() {
    // The previous 2 s default against the largest (2 s) message: a consumer
    // playing in real time is mistaken for one that stopped.
    let timeouts = Timeouts {
        deliver: Duration::from_millis(1_500),
        ..Timeouts::default()
    };
    let (mut connection, mut server) = mock(timeouts).await;
    let request = ended_turn(&mut connection, &mut server, 1).await;
    let service = tokio::spawn(speak(
        server,
        Answer {
            seconds: 60,
            chunk: LARGEST,
            speed: 10,
            stall: None,
            settle: true,
        },
    ));
    let mut delivered = 0;
    while let Some(event) = connection.next_event().await {
        if let Event::Audio { lineage, pcm, .. } = event {
            assert_eq!(lineage.request, request);
            delivered += pcm.len() as u64;
            sleep(playing_time(pcm.len() as u64)).await;
        }
    }
    assert_eq!(connection.state(), State::Disconnected(Error::Backpressure));
    assert!(delivered < 60 * RATE);
    service.abort();
}

#[tokio::test(start_paused = true)]
async fn full_buffer_holds_the_service_by_flow_control_without_false_timeouts() {
    // A 10 s buffer against 90 s generated at fifty times speaking speed. The
    // socket goes unread for long stretches; neither the stall nor the read
    // bound may fire, and nothing is dropped.
    let configuration = configuration(Timeouts::default())
        .output_buffer_seconds(10)
        .unwrap();
    let (mut connection, mut server) = mock_with(configuration).await;
    let request = ended_turn(&mut connection, &mut server, 1).await;
    let service = tokio::spawn(speak(
        server,
        Answer {
            seconds: 90,
            chunk: LARGEST,
            speed: 50,
            stall: None,
            settle: true,
        },
    ));
    // Playback also pauses for 25 s while the buffer is full.
    let pause = Some((20 * RATE, Duration::from_secs(25)));
    let played = {
        let player = play(&mut connection, request, pause);
        tokio::pin!(player);
        // Well after generation would have finished, the service is still held.
        tokio::select! {
            _ = &mut player => panic!("playback cannot finish this early"),
            _ = sleep(Duration::from_secs(40)) => assert!(!service.is_finished()),
        }
        player.await
    };
    assert_eq!(played.samples, 90 * RATE);
    assert_eq!(played.took, Duration::from_secs(115));
    let spoken = service.await.unwrap();
    // Unheld, generation takes 1.8 s. It was held for most of the playback:
    // beyond the 10 s buffer only the 16-event queue and the socket hold audio.
    assert!(
        spoken.written_in >= Duration::from_secs(55),
        "{:?}",
        spoken.written_in
    );
    assert_eq!(connection.state(), State::Ready);
}

#[tokio::test(start_paused = true)]
async fn interrupting_a_long_buffered_answer_frees_it_and_answers_the_new_question_at_once() {
    let (mut connection, mut server) = mock(Timeouts::default()).await;
    let old = ended_turn(&mut connection, &mut server, 1).await;
    // Two minutes generated in twelve seconds; only five seconds are played.
    let service = tokio::spawn(speak(
        server,
        Answer {
            seconds: 120,
            chunk: LARGEST,
            speed: 10,
            stall: None,
            settle: false,
        },
    ));
    let mut heard = 0u64;
    while heard < 5 * RATE {
        let Event::Audio { lineage, pcm, .. } = event(&mut connection).await else {
            panic!("audio first");
        };
        assert_eq!(lineage.request, old);
        heard += pcm.len() as u64;
        sleep(playing_time(pcm.len() as u64)).await;
    }
    sleep(Duration::from_secs(10)).await;
    // Everything else is generated and waiting. The person interrupts.
    let mut server = service.await.unwrap().server;
    let new = request(2);
    let input = connection.input();
    let asked = Clock::now();
    input.try_start(new).unwrap();
    input.try_audio(new, 0, Instant::now(), &[7; 160]).unwrap();
    input.try_end(new).unwrap();
    sent(&mut server, "activityStart").await;
    sent(&mut server, "audio").await;
    send_json(&mut server, json!({"serverContent":{"interrupted":true}})).await;
    send_json(&mut server, idle()).await;
    sent(&mut server, "activityEnd").await;
    send_json(&mut server, ramp(0, 240)).await;
    // No block of the old answer is delivered after the interruption: the
    // next events are the new request's own, without playing through or
    // skipping past 100 s of stale audio one event at a time.
    let mut order = Vec::new();
    loop {
        match event(&mut connection).await {
            Event::InputStarted { lineage, .. } => order.push(("started", lineage.request)),
            Event::Interrupted { lineage } => order.push(("interrupted", lineage.request)),
            Event::TurnComplete { lineage, .. } => order.push(("complete", lineage.request)),
            Event::GenerationComplete { lineage } => order.push(("generated", lineage.request)),
            Event::Audio { lineage, .. } => {
                order.push(("audio", lineage.request));
                break;
            }
            other => panic!("unexpected event: {other:?}"),
        }
    }
    assert_eq!(order.last(), Some(&("audio", new)));
    assert!(!order.contains(&("audio", old)));
    assert!(
        asked.elapsed() < Duration::from_millis(200),
        "{:?}",
        asked.elapsed()
    );
    assert_eq!(connection.state(), State::Ready);
}
