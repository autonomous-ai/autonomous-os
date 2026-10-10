//! A person who keeps talking: continuing a question after the endpointer cut
//! it, interrupting twice, or changing topic before any answer has started.
//!
//! What the service does when asked to interrupt a response it has not begun
//! is not documented and has not been observed. Each scenario therefore runs
//! against every plausible service behavior. Where the service stays silent
//! the outcome depends on [`BarrierPolicy`] and is reported, never guessed
//! silently. The clock is paused: every bound below is exact.
use super::*;
use tokio::time::{Instant as Clock, sleep};

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
enum Seen {
    Start,
    End,
    /// An input block, identified by the value of its samples.
    Block(i16),
}
/// The next `count` client messages as the service received them, in order.
async fn seen(server: &mut MockSocket, count: usize) -> Vec<Seen> {
    let mut all = Vec::with_capacity(count);
    for _ in 0..count {
        let message = receive(server).await;
        let input = &message["realtimeInput"];
        all.push(if input.get("activityStart").is_some() {
            Seen::Start
        } else if input.get("activityEnd").is_some() {
            Seen::End
        } else {
            let pcm = STANDARD
                .decode(input["audio"]["data"].as_str().expect("an audio block"))
                .unwrap();
            assert_eq!(pcm.len(), 320);
            let value = i16::from_le_bytes([pcm[0], pcm[1]]);
            assert!(
                pcm.chunks_exact(2)
                    .all(|pair| i16::from_le_bytes([pair[0], pair[1]]) == value)
            );
            Seen::Block(value)
        });
    }
    all
}
fn blocks(values: std::ops::Range<i16>) -> Vec<Seen> {
    values.map(Seen::Block).collect()
}
/// Offer consecutive 10 ms blocks whose samples carry the given values.
fn say(input: &InputSender, id: u64, first_sequence: u64, values: std::ops::Range<i16>) {
    for (offset, value) in values.enumerate() {
        input
            .try_audio(
                request(id),
                first_sequence + offset as u64,
                Instant::now(),
                &[value; 160],
            )
            .unwrap();
    }
}
fn interrupted() -> Value {
    json!({"serverContent":{"interrupted":true}})
}
fn policy(policy: BarrierPolicy) -> SessionConfig {
    configuration(Timeouts::default()).barrier_policy(policy)
}
/// The first question, cut by the endpointer before the person had finished:
/// blocks 10..14, committed, no answer yet.
async fn endpointed(connection: &mut Connection, server: &mut MockSocket) {
    let input = connection.input();
    input.try_start(request(1)).unwrap();
    say(&input, 1, 0, 10..14);
    input.try_end(request(1)).unwrap();
    let mut expected = vec![Seen::Start];
    expected.extend(blocks(10..14));
    expected.push(Seen::End);
    assert_eq!(seen(server, 6).await, expected);
    assert!(matches!(
        event(connection).await,
        Event::InputStarted {
            waiting_for_barrier: false,
            ..
        }
    ));
}
/// The person resumes: request 2 opens with blocks 20..26 and ends.
async fn resumed(connection: &mut Connection, server: &mut MockSocket) {
    let input = connection.input();
    input.try_start(request(2)).unwrap();
    say(&input, 2, 0, 20..26);
    input.try_end(request(2)).unwrap();
    // The interruption request, then the opening words: complete, in order,
    // once, and before the service has confirmed anything.
    let mut expected = vec![Seen::Start];
    expected.extend(blocks(20..26));
    assert_eq!(seen(server, 7).await, expected);
    assert!(matches!(
        event(connection).await,
        Event::InputStarted { lineage, waiting_for_barrier: true } if lineage.request == request(2)
    ));
    // Not committed: the service has not been asked to answer it yet.
    silent(server).await;
}
/// The answer to request 2, attributed to it and to nothing else.
async fn answered(connection: &mut Connection, server: &mut MockSocket, id: u64) {
    send_json(server, audio(77)).await;
    send_json(server, generated()).await;
    send_json(server, idle()).await;
    assert!(matches!(
        event(connection).await,
        Event::Audio { lineage, sequence: 0, pcm } if lineage.request == request(id) && pcm == [77]
    ));
    assert!(matches!(
        event(connection).await,
        Event::GenerationComplete { lineage } if lineage.request == request(id)
    ));
    assert!(matches!(
        event(connection).await,
        Event::TurnComplete { lineage, idle: true } if lineage.request == request(id)
    ));
    assert_eq!(connection.state(), State::Ready);
}

#[tokio::test(start_paused = true)]
async fn continued_question_is_answered_once_when_the_service_confirms_the_interruption() {
    // Confirmed with `interrupted` then completion, or with completion alone.
    for confirmation in [vec![interrupted(), idle()], vec![idle()]] {
        for policy_choice in [BarrierPolicy::Require, BarrierPolicy::AssumeAfterQuiet] {
            let (mut connection, mut server) = mock_with(policy(policy_choice)).await;
            endpointed(&mut connection, &mut server).await;
            resumed(&mut connection, &mut server).await;
            let confirmed = confirmation.len();
            for message in confirmation.clone() {
                send_json(&mut server, message).await;
            }
            // The first question's lifecycle is reported under its own name.
            if confirmed == 2 {
                assert!(matches!(
                    event(&mut connection).await,
                    Event::Interrupted { lineage } if lineage.request == request(1)
                ));
            }
            assert!(matches!(
                event(&mut connection).await,
                Event::TurnComplete { lineage, idle: true } if lineage.request == request(1)
            ));
            assert_eq!(seen(&mut server, 1).await, [Seen::End]);
            answered(&mut connection, &mut server, 2).await;
        }
    }
}

#[tokio::test(start_paused = true)]
async fn answer_to_the_cut_question_that_raced_the_resumption_is_never_played() {
    let (mut connection, mut server) = mock_with(policy(BarrierPolicy::Require)).await;
    endpointed(&mut connection, &mut server).await;
    resumed(&mut connection, &mut server).await;
    // The service had just started answering the fragment when it was told.
    send_json(&mut server, audio(11)).await;
    send_json(&mut server, audio(12)).await;
    send_json(&mut server, interrupted()).await;
    send_json(&mut server, idle()).await;
    assert!(matches!(
        event(&mut connection).await,
        Event::Interrupted { lineage } if lineage.request == request(1)
    ));
    assert!(matches!(
        event(&mut connection).await,
        Event::TurnComplete { lineage, idle: true } if lineage.request == request(1)
    ));
    assert_eq!(seen(&mut server, 1).await, [Seen::End]);
    answered(&mut connection, &mut server, 2).await;
}

#[tokio::test(start_paused = true)]
async fn silent_service_ends_the_session_under_require_and_is_flagged_under_assume() {
    let (mut connection, mut server) = mock_with(policy(BarrierPolicy::Require)).await;
    endpointed(&mut connection, &mut server).await;
    let resumed_at = Clock::now();
    resumed(&mut connection, &mut server).await;
    assert_eq!(disconnected(&connection).await, Error::BarrierTimeout);
    assert!(resumed_at.elapsed() >= Timeouts::default().barrier);

    let (mut connection, mut server) = mock_with(policy(BarrierPolicy::AssumeAfterQuiet)).await;
    endpointed(&mut connection, &mut server).await;
    let resumed_at = Clock::now();
    resumed(&mut connection, &mut server).await;
    // Nothing for the whole bound. The assumption is reported before the
    // request is committed and before any of its answer can exist.
    assert!(matches!(
        event(&mut connection).await,
        Event::BarrierAssumed { superseded: Some(old), successor: Some(new), .. }
            if old == request(1) && new == request(2)
    ));
    let waited = resumed_at.elapsed();
    assert!(
        waited >= Timeouts::default().barrier
            && waited < Timeouts::default().barrier + Duration::from_millis(200),
        "{waited:?}"
    );
    assert_eq!(seen(&mut server, 1).await, [Seen::End]);
    answered(&mut connection, &mut server, 2).await;
}

#[tokio::test(start_paused = true)]
async fn late_answer_to_the_cut_question_rearms_the_barrier_while_the_person_is_still_speaking() {
    let (mut connection, mut server) = mock_with(policy(BarrierPolicy::AssumeAfterQuiet)).await;
    endpointed(&mut connection, &mut server).await;
    // The person resumes and keeps talking past the barrier bound.
    let input = connection.input();
    input.try_start(request(2)).unwrap();
    say(&input, 2, 0, 20..23);
    let mut expected = vec![Seen::Start];
    expected.extend(blocks(20..23));
    assert_eq!(seen(&mut server, 4).await, expected);
    event(&mut connection).await;
    assert!(matches!(
        timeout(Duration::from_secs(5), connection.next_event()).await,
        Ok(Some(Event::BarrierAssumed { .. }))
    ));
    // The assumption was wrong: the service answers the fragment after all.
    send_json(&mut server, audio(11)).await;
    discarded(&mut connection, Discard::LateOutput).await;
    say(&input, 2, 3, 23..26);
    assert_eq!(seen(&mut server, 3).await, blocks(23..26));
    input.try_end(request(2)).unwrap();
    // Evidence beats assumption: the End is withheld again while the old
    // response is still producing, however long that takes.
    send_json(&mut server, audio(12)).await;
    discarded(&mut connection, Discard::LateOutput).await;
    sleep(Duration::from_secs(1)).await;
    send_json(&mut server, audio(13)).await;
    discarded(&mut connection, Discard::LateOutput).await;
    silent(&mut server).await;
    send_json(&mut server, idle()).await;
    discarded(&mut connection, Discard::LateTerminal).await;
    assert_eq!(seen(&mut server, 1).await, [Seen::End]);
    answered(&mut connection, &mut server, 2).await;
}

#[tokio::test(start_paused = true)]
async fn answer_that_starts_only_after_an_assumed_barrier_is_committed_is_flagged_not_prevented() {
    // The one case this transport cannot resolve: the service neither
    // confirms nor produces anything for the whole bound, the next request is
    // committed, and only then does an answer arrive. Nothing on the wire says
    // which request it answers. It is delivered under the committed request,
    // after an explicit BarrierAssumed for exactly that request.
    let (mut connection, mut server) = mock_with(policy(BarrierPolicy::AssumeAfterQuiet)).await;
    endpointed(&mut connection, &mut server).await;
    resumed(&mut connection, &mut server).await;
    assert!(matches!(
        event(&mut connection).await,
        Event::BarrierAssumed { successor: Some(new), .. } if new == request(2)
    ));
    assert_eq!(seen(&mut server, 1).await, [Seen::End]);
    send_json(&mut server, audio(11)).await;
    assert!(matches!(
        event(&mut connection).await,
        Event::Audio { lineage, .. } if lineage.request == request(2)
    ));
}

#[tokio::test(start_paused = true)]
async fn interrupting_twice_keeps_every_opening_word_and_answers_only_the_last_request() {
    let (mut connection, mut server) = mock_with(policy(BarrierPolicy::Require)).await;
    endpointed(&mut connection, &mut server).await;
    // The first answer is under way when the person interrupts.
    send_json(&mut server, audio(1)).await;
    assert!(matches!(event(&mut connection).await, Event::Audio { .. }));
    let input = connection.input();
    input.try_start(request(2)).unwrap();
    say(&input, 2, 0, 20..24);
    input.try_end(request(2)).unwrap();
    let mut expected = vec![Seen::Start];
    expected.extend(blocks(20..24));
    assert_eq!(seen(&mut server, 5).await, expected);
    silent(&mut server).await;
    // Before the service confirms, the person starts over. One interruption
    // request was enough: the new opening continues the same activity, and
    // the superseded request is never committed on its own.
    input.try_start(request(3)).unwrap();
    say(&input, 3, 0, 30..35);
    assert_eq!(seen(&mut server, 5).await, blocks(30..35));
    for id in [2, 3] {
        assert!(matches!(
            event(&mut connection).await,
            Event::InputStarted { lineage, waiting_for_barrier: true } if lineage.request == request(id)
        ));
    }
    send_json(&mut server, audio(2)).await;
    send_json(&mut server, interrupted()).await;
    send_json(&mut server, idle()).await;
    assert!(matches!(
        event(&mut connection).await,
        Event::Interrupted { lineage } if lineage.request == request(1)
    ));
    assert!(matches!(
        event(&mut connection).await,
        Event::TurnComplete { lineage, .. } if lineage.request == request(1)
    ));
    // Confirmed, but the person is still speaking: no End yet.
    silent(&mut server).await;
    say(&input, 3, 5, 35..37);
    input.try_end(request(3)).unwrap();
    let mut expected = blocks(35..37);
    expected.push(Seen::End);
    assert_eq!(seen(&mut server, 3).await, expected);
    answered(&mut connection, &mut server, 3).await;
}

#[tokio::test(start_paused = true)]
async fn second_interruption_of_a_committed_unanswered_request_waits_for_its_own_barrier() {
    let (mut connection, mut server) = mock_with(policy(BarrierPolicy::AssumeAfterQuiet)).await;
    endpointed(&mut connection, &mut server).await;
    resumed(&mut connection, &mut server).await;
    send_json(&mut server, idle()).await;
    event(&mut connection).await;
    // Request 2 is committed and unanswered when the person speaks again.
    assert_eq!(seen(&mut server, 1).await, [Seen::End]);
    let input = connection.input();
    input.try_start(request(3)).unwrap();
    say(&input, 3, 0, 30..33);
    input.try_end(request(3)).unwrap();
    let mut expected = vec![Seen::Start];
    expected.extend(blocks(30..33));
    assert_eq!(seen(&mut server, 4).await, expected);
    assert!(matches!(
        event(&mut connection).await,
        Event::InputStarted { lineage, waiting_for_barrier: true } if lineage.request == request(3)
    ));
    // This time the service answers request 2 briefly before settling.
    send_json(&mut server, audio(22)).await;
    send_json(&mut server, interrupted()).await;
    send_json(&mut server, idle()).await;
    assert!(matches!(
        event(&mut connection).await,
        Event::Interrupted { lineage } if lineage.request == request(2)
    ));
    assert!(matches!(
        event(&mut connection).await,
        Event::TurnComplete { lineage, .. } if lineage.request == request(2)
    ));
    assert_eq!(seen(&mut server, 1).await, [Seen::End]);
    answered(&mut connection, &mut server, 3).await;
}

#[tokio::test(start_paused = true)]
async fn topic_change_before_any_answer_drops_the_first_topic_answer_whenever_it_arrives() {
    // The first topic's answer arrives complete, after the new topic began and
    // without any interruption notice: the service simply finished it.
    let (mut connection, mut server) = mock_with(policy(BarrierPolicy::Require)).await;
    endpointed(&mut connection, &mut server).await;
    resumed(&mut connection, &mut server).await;
    send_json(&mut server, audio(11)).await;
    sleep(Duration::from_millis(1_500)).await;
    send_json(&mut server, audio(12)).await;
    sleep(Duration::from_millis(1_500)).await;
    // Three seconds in, longer than the bound, but never silent that long.
    assert_eq!(connection.state(), State::Ready);
    send_json(&mut server, generated()).await;
    send_json(&mut server, idle()).await;
    assert!(matches!(
        event(&mut connection).await,
        Event::TurnComplete { lineage, idle: true } if lineage.request == request(1)
    ));
    // Only now is the new topic committed, and only its answer is delivered.
    assert_eq!(seen(&mut server, 1).await, [Seen::End]);
    answered(&mut connection, &mut server, 2).await;
}

#[tokio::test(start_paused = true)]
async fn stray_output_with_no_request_holds_the_next_commit_until_it_settles() {
    let (mut connection, mut server) = mock_with(policy(BarrierPolicy::AssumeAfterQuiet)).await;
    endpointed(&mut connection, &mut server).await;
    send_json(&mut server, audio(1)).await;
    send_json(&mut server, idle()).await;
    event(&mut connection).await;
    event(&mut connection).await;
    // After its idle completion the service sends more of the same answer.
    send_json(&mut server, audio(2)).await;
    discarded(&mut connection, Discard::UnownedOutput).await;
    let input = connection.input();
    input.try_start(request(2)).unwrap();
    say(&input, 2, 0, 20..22);
    input.try_end(request(2)).unwrap();
    let mut expected = vec![Seen::Start];
    expected.extend(blocks(20..22));
    assert_eq!(seen(&mut server, 3).await, expected);
    assert!(matches!(
        event(&mut connection).await,
        Event::InputStarted {
            waiting_for_barrier: true,
            ..
        }
    ));
    send_json(&mut server, audio(3)).await;
    discarded(&mut connection, Discard::LateOutput).await;
    silent(&mut server).await;
    // It never says it finished. After the bound of silence the request
    // proceeds, flagged, with no earlier request to name.
    assert!(matches!(
        event(&mut connection).await,
        Event::BarrierAssumed { superseded: None, successor: Some(new), .. } if new == request(2)
    ));
    assert_eq!(seen(&mut server, 1).await, [Seen::End]);
    answered(&mut connection, &mut server, 2).await;
}

#[tokio::test(start_paused = true)]
async fn input_of_a_superseded_request_that_has_not_reached_the_wire_is_dropped_not_reordered() {
    let (mut connection, mut server) = mock_with(policy(BarrierPolicy::Require)).await;
    let input = connection.input();
    // Everything is offered before the transport runs at all. The first
    // request is cancelled and closed, as the provider worker does, the
    // instant the second is admitted.
    input.try_start(request(1)).unwrap();
    say(&input, 1, 0, 10..14);
    input.retire(request(1));
    input.try_end(request(1)).unwrap();
    input.try_start(request(2)).unwrap();
    say(&input, 2, 0, 20..23);
    input.try_end(request(2)).unwrap();
    let mut expected = vec![Seen::Start];
    expected.extend(blocks(20..23));
    expected.push(Seen::End);
    // One activity, holding only the surviving request's words in order. The
    // cancelled request is neither sent late nor committed.
    assert_eq!(seen(&mut server, 5).await, expected);
    for id in [1, 2] {
        assert!(matches!(
            event(&mut connection).await,
            Event::InputStarted { lineage, waiting_for_barrier: false } if lineage.request == request(id)
        ));
    }
    answered(&mut connection, &mut server, 2).await;
}

#[tokio::test(start_paused = true)]
async fn earlier_response_that_never_stops_cannot_hold_the_next_request_forever() {
    for policy_choice in [BarrierPolicy::Require, BarrierPolicy::AssumeAfterQuiet] {
        let (mut connection, mut server) = mock_with(policy(policy_choice)).await;
        endpointed(&mut connection, &mut server).await;
        let resumed_at = Clock::now();
        resumed(&mut connection, &mut server).await;
        // Asked to stop, the service keeps producing the first answer: never
        // silent for the barrier bound, never settled.
        let streaming = async {
            loop {
                if server
                    .send(Message::text(audio(11).to_string()))
                    .await
                    .is_err()
                {
                    return;
                }
                sleep(Duration::from_millis(500)).await;
            }
        };
        let failed = async {
            let mut state = connection.subscribe_state();
            let failed = state.wait_for(|state| matches!(state, State::Disconnected(_)));
            *timeout(Duration::from_secs(120), failed)
                .await
                .unwrap()
                .unwrap()
        };
        tokio::select! {
            _ = streaming => panic!("the connection closed before it was declared failed"),
            state = failed => assert_eq!(state, State::Disconnected(Error::BarrierTimeout)),
        }
        // Bounded by the same limit as a response that never starts.
        let waited = resumed_at.elapsed();
        let bound = Timeouts::default().response;
        assert!(
            waited >= bound && waited < bound + Duration::from_secs(1),
            "{waited:?}"
        );
    }
}
