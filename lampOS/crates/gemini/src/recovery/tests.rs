use super::*;

mod availability;
mod context;
mod drain;
mod soak;
use crate::{
    Credential, GOOGLE_ENDPOINT, Resumption, Timeouts,
    testing::{Dial, FakeConnector, audio, connector, dial_within, next_dial},
};
use serde_json::{Value, json};
use tokio::{sync::mpsc::UnboundedReceiver, time::timeout};

const WAIT: Duration = Duration::from_secs(3);

fn config() -> SessionConfig {
    SessionConfig::new(GOOGLE_ENDPOINT, Credential::api_key("test-only").unwrap())
        .unwrap()
        .resumption(Resumption::Retain)
}
fn quick() -> RecoveryPolicy {
    RecoveryPolicy {
        first_backoff: Duration::from_millis(20),
        max_backoff: Duration::from_millis(40),
        outage_budget: Duration::from_secs(2),
        ..RecoveryPolicy::default()
    }
}
fn request(id: u64) -> RequestId {
    RequestId::new(id).unwrap()
}
fn idle() -> Value {
    json!({"serverContent":{"turnComplete":true,"interactionStatus":"IDLE"}})
}
fn handle(value: &str) -> Value {
    json!({"sessionResumptionUpdate":{"newHandle":value,"resumable":true}})
}
type Rig = (Supervisor<FakeConnector>, UnboundedReceiver<Dial>);

/// The next notice other than the outage announcement, which has its own
/// tests: when it arrives relative to buffered output depends on scheduling.
async fn notice(supervisor: &mut Supervisor<FakeConnector>) -> Notice {
    loop {
        match raw(supervisor).await.expect("no failure") {
            Notice::Unavailable { .. } => {}
            other => return other,
        }
    }
}
async fn raw(supervisor: &mut Supervisor<FakeConnector>) -> std::result::Result<Notice, Failure> {
    timeout(WAIT, supervisor.next())
        .await
        .expect("notice in time")
}
async fn failure(supervisor: &mut Supervisor<FakeConnector>) -> Failure {
    loop {
        match raw(supervisor).await {
            Ok(Notice::Unavailable { .. }) => {}
            Ok(other) => panic!("expected a terminal failure, got {other:?}"),
            Err(failure) => return failure,
        }
    }
}
/// Drive the supervisor until the scripted service has accepted its next dial.
async fn ready(rig: &mut Rig) -> (Notice, Dial, Value) {
    let (supervisor, dials) = rig;
    let serve = async {
        let mut dial = next_dial(dials).await;
        let setup = dial.service.accept().await;
        (dial, setup)
    };
    let (notice, (dial, setup)) = tokio::join!(notice(supervisor), serve);
    (notice, dial, setup)
}
/// A connected supervisor whose service has already issued a current handle.
async fn connected(policy: RecoveryPolicy, config: SessionConfig) -> (Rig, Dial) {
    let (dialer, dials) = connector(config);
    let mut rig = (Supervisor::new(dialer, policy).unwrap(), dials);
    let (first, mut dial, _) = ready(&mut rig).await;
    assert!(matches!(
        first,
        Notice::Ready {
            context: Context::Initial,
            attempts: 1,
            after: None,
            ..
        }
    ));
    dial.service.send(handle("HANDLE-ONE")).await;
    (rig, dial)
}
/// Start, send one block and optionally end input for `id`.
async fn speak(rig: &mut Rig, dial: &mut Dial, id: u64, end: bool) {
    let supervisor = &mut rig.0;
    supervisor.try_start(request(id)).unwrap();
    assert!(
        dial.service.receive().await["realtimeInput"]
            .get("activityStart")
            .is_some()
    );
    assert!(matches!(
        notice(supervisor).await,
        Notice::Event(Event::InputStarted { lineage, .. }) if lineage.request.get() == id
    ));
    supervisor
        .try_audio(request(id), 0, std::time::Instant::now(), &[1; 160])
        .unwrap();
    assert!(
        dial.service.receive().await["realtimeInput"]
            .get("audio")
            .is_some()
    );
    if end {
        supervisor.try_end(request(id)).unwrap();
        assert!(
            dial.service.receive().await["realtimeInput"]
                .get("activityEnd")
                .is_some()
        );
    }
}

#[tokio::test]
async fn idle_disconnect_resumes_the_conversation_on_a_fresh_session() {
    let (mut rig, mut dial) = connected(quick(), config()).await;
    assert!(!dial.resumed);
    // The measured V1 failure: the service closes a session nobody talks to.
    dial.service.close(1008, "The operation was aborted").await;
    let (second, redial, setup) = ready(&mut rig).await;
    assert!(matches!(
        second,
        Notice::Ready {
            session,
            context: Context::Resumed,
            attempts: 1,
            after: Some(Error::PeerClosed { code: Some(1008) }),
            ..
        } if session.get() == 2
    ));
    assert!(redial.resumed);
    assert_eq!(setup["setup"]["sessionResumption"]["handle"], "HANDLE-ONE");
    assert!(rig.0.is_connected());
}

#[tokio::test]
async fn initial_setup_is_unchanged_and_handles_are_requested_only_on_demand() {
    let (dialer, dials) = connector(config());
    let mut rig = (Supervisor::new(dialer, quick()).unwrap(), dials);
    let (_, _dial, setup) = ready(&mut rig).await;
    assert!(setup["setup"].get("sessionResumption").is_none());

    let (dialer, dials) = connector(config().resumption(Resumption::Request));
    let mut rig = (Supervisor::new(dialer, quick()).unwrap(), dials);
    let (_, _dial, setup) = ready(&mut rig).await;
    assert_eq!(setup["setup"]["sessionResumption"], json!({}));
}

#[tokio::test]
async fn without_a_handle_recovery_is_refused_unless_policy_accepts_lost_context() {
    for require_context in [true, false] {
        let policy = RecoveryPolicy {
            require_context,
            ..quick()
        };
        let (dialer, dials) = connector(config());
        let mut rig = (Supervisor::new(dialer, policy).unwrap(), dials);
        let (_, mut dial, _) = ready(&mut rig).await;
        dial.service.close(1008, "idle").await;
        if require_context {
            assert_eq!(
                failure(&mut rig.0).await,
                Failure {
                    error: Error::PeerClosed { code: Some(1008) },
                    reason: FailureReason::NoResumableContext,
                    attempts: 0,
                }
            );
            assert!(
                timeout(Duration::from_millis(60), rig.1.recv())
                    .await
                    .is_err()
            );
            continue;
        }
        let (second, redial, setup) = ready(&mut rig).await;
        // Reported as a forgotten conversation, never as continuity.
        assert!(matches!(
            second,
            Notice::Ready {
                context: Context::Fresh,
                ..
            }
        ));
        assert!(!redial.resumed);
        assert!(setup["setup"].get("sessionResumption").is_none());
    }
}

#[tokio::test]
async fn a_handle_older_than_the_latest_exchange_is_reported_as_partial_context() {
    let (mut rig, mut dial) = connected(quick(), config()).await;
    dial.service
        .send(json!({"sessionResumptionUpdate":{"newHandle":"","resumable":false}}))
        .await;
    dial.service.close(1011, "internal").await;
    let (second, redial, setup) = ready(&mut rig).await;
    assert!(matches!(
        second,
        Notice::Ready {
            context: Context::ResumedBeforeLatest,
            ..
        }
    ));
    assert!(redial.resumed);
    assert_eq!(setup["setup"]["sessionResumption"]["handle"], "HANDLE-ONE");
}

#[tokio::test]
async fn unfinished_request_is_reported_lost_at_its_stage_and_never_replayed() {
    for stage in [Stage::InputOpen, Stage::AwaitingResponse, Stage::Responding] {
        let (mut rig, mut dial) = connected(quick(), config()).await;
        speak(&mut rig, &mut dial, 1, stage != Stage::InputOpen).await;
        if stage == Stage::Responding {
            dial.service.send(audio(7, 240)).await;
            assert!(matches!(
                notice(&mut rig.0).await,
                Notice::Event(Event::Audio { .. })
            ));
        }
        dial.service.close(1011, "internal").await;
        assert!(matches!(
            notice(&mut rig.0).await,
            Notice::TurnLost { lineage, stage: lost, error: Error::PeerClosed { code: Some(1011) } }
                if lineage.request.get() == 1 && lineage.session.get() == 1 && lost == stage
        ));
        // The link still heals for the next request, but nothing of the lost
        // one is sent again: no input replay and no second answer.
        let (second, mut redial, _) = ready(&mut rig).await;
        assert!(matches!(second, Notice::Ready { after: Some(_), .. }));
        assert!(
            redial
                .service
                .try_receive(Duration::from_millis(80))
                .await
                .is_none()
        );
        assert_eq!(rig.0.try_start(request(1)), Err(Error::StaleRequest));
        rig.0.try_start(request(2)).unwrap();
    }
}

#[tokio::test]
async fn a_generated_answer_is_settled_by_the_closed_connection_not_lost() {
    let (mut rig, mut dial) = connected(quick(), config()).await;
    speak(&mut rig, &mut dial, 1, true).await;
    dial.service.send(audio(7, 240)).await;
    dial.service
        .send(json!({"serverContent":{"generationComplete":true}}))
        .await;
    // The idle completion never arrives: the service closes first.
    dial.service.close(1008, "idle").await;
    assert!(matches!(
        notice(&mut rig.0).await,
        Notice::Event(Event::Audio { .. })
    ));
    assert!(matches!(
        notice(&mut rig.0).await,
        Notice::Event(Event::GenerationComplete { .. })
    ));
    assert!(matches!(
        notice(&mut rig.0).await,
        Notice::Settled { lineage, error: Error::PeerClosed { code: Some(1008) } }
            if lineage.request.get() == 1
    ));
    let (second, _redial, _) = ready(&mut rig).await;
    assert!(matches!(
        second,
        Notice::Ready {
            context: Context::Resumed,
            ..
        }
    ));
}

#[tokio::test]
async fn completed_and_locally_cancelled_requests_are_not_reported_lost() {
    // Completed: the idle barrier arrived before the close.
    let (mut rig, mut dial) = connected(quick(), config()).await;
    speak(&mut rig, &mut dial, 1, true).await;
    dial.service.send(audio(7, 240)).await;
    dial.service.send(idle()).await;
    dial.service.close(1008, "idle").await;
    assert!(matches!(
        notice(&mut rig.0).await,
        Notice::Event(Event::Audio { .. })
    ));
    assert!(matches!(
        notice(&mut rig.0).await,
        Notice::Event(Event::TurnComplete { idle: true, .. })
    ));
    let (second, _redial, _) = ready(&mut rig).await;
    assert!(matches!(second, Notice::Ready { after: Some(_), .. }));

    // Cancelled locally: nothing was owed when the connection ended.
    let (mut rig, mut dial) = connected(quick(), config()).await;
    speak(&mut rig, &mut dial, 1, true).await;
    rig.0.retire(request(1));
    dial.service.close(1011, "internal").await;
    let (second, _redial, _) = ready(&mut rig).await;
    assert!(matches!(second, Notice::Ready { after: Some(_), .. }));
    // Retirement is remembered by the new connection.
    assert_eq!(rig.0.try_start(request(1)), Err(Error::StaleRequest));
}

#[tokio::test]
async fn stalled_response_is_a_bounded_reported_loss_then_recovery() {
    let timeouts = Timeouts {
        stall: Duration::from_millis(80),
        ..Timeouts::default()
    };
    let (mut rig, mut dial) = connected(quick(), config().timeouts(timeouts).unwrap()).await;
    speak(&mut rig, &mut dial, 1, true).await;
    dial.service.send(audio(7, 240)).await;
    assert!(matches!(
        notice(&mut rig.0).await,
        Notice::Event(Event::Audio { .. })
    ));
    // The service goes quiet mid-answer without closing.
    assert!(matches!(
        notice(&mut rig.0).await,
        Notice::TurnLost {
            stage: Stage::Responding,
            error: Error::StalledResponse,
            ..
        }
    ));
    let (second, _redial, _) = ready(&mut rig).await;
    assert!(matches!(
        second,
        Notice::Ready {
            after: Some(Error::StalledResponse),
            ..
        }
    ));
}

#[tokio::test]
async fn input_is_refused_not_buffered_while_disconnected_and_control_is_never_blocked() {
    let (mut rig, mut dial) = connected(quick(), config()).await;
    dial.service.close(1008, "idle").await;
    // The outage is announced at once, with its fixed category.
    assert!(matches!(
        raw(&mut rig.0).await,
        Ok(Notice::Unavailable {
            error: Error::PeerClosed { code: Some(1008) }
        })
    ));
    // The reconnect is in flight and unanswered. Polling returns control to
    // the caller every time; nothing here waits on the network.
    for _ in 0..3 {
        assert!(
            timeout(Duration::from_millis(20), rig.0.next())
                .await
                .is_err()
        );
        assert!(!rig.0.is_connected());
        assert_eq!(rig.0.try_start(request(1)), Err(Error::NotConnected));
        assert_eq!(
            rig.0
                .try_audio(request(1), 0, std::time::Instant::now(), &[0; 160]),
            Err(Error::NotConnected)
        );
        assert_eq!(rig.0.try_end(request(1)), Err(Error::NotConnected));
        rig.0.retire(request(1));
    }
    let (second, _redial, _) = ready(&mut rig).await;
    assert!(matches!(second, Notice::Ready { attempts: 1, .. }));
    assert_eq!(rig.0.try_start(request(1)), Err(Error::StaleRequest));
    rig.0.try_start(request(2)).unwrap();
}

#[tokio::test]
async fn reconnect_attempts_backoff_and_outage_budget_are_bounded() {
    let policy = RecoveryPolicy {
        max_attempts: 3,
        ..quick()
    };
    let (mut rig, mut dial) = connected(policy, config()).await;
    dial.service.close(1008, "idle").await;
    let started = Instant::now();
    let (supervisor, dials) = (&mut rig.0, &mut rig.1);
    let refuse = async {
        for _ in 0..3 {
            // Dropping the service before setup fails that attempt.
            drop(next_dial(dials).await);
        }
    };
    let (ended, ()) = tokio::join!(failure(supervisor), refuse);
    assert_eq!(ended.reason, FailureReason::AttemptsExhausted);
    assert_eq!(ended.attempts, 3);
    assert_eq!(ended.error.recovery(), Recovery::Reconnect);
    // 20 ms then 40 ms were waited between the three attempts.
    assert!(started.elapsed() >= Duration::from_millis(60));
    assert!(
        timeout(Duration::from_millis(80), dials.recv())
            .await
            .is_err()
    );
    assert_eq!(failure(supervisor).await, ended);

    // An attempt that never completes is cut by the outage budget.
    let policy = RecoveryPolicy {
        outage_budget: Duration::from_millis(150),
        ..quick()
    };
    let (mut rig, mut dial) = connected(policy, config()).await;
    dial.service.close(1008, "idle").await;
    let started = Instant::now();
    let (supervisor, dials) = (&mut rig.0, &mut rig.1);
    let hang = async { next_dial(dials).await };
    let (ended, _held) = tokio::join!(failure(supervisor), hang);
    assert_eq!(
        ended,
        Failure {
            error: Error::PeerClosed { code: Some(1008) },
            reason: FailureReason::BudgetExhausted,
            attempts: 1,
        }
    );
    assert!(started.elapsed() >= Duration::from_millis(150));
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[tokio::test]
async fn repeated_recoveries_inside_the_window_end_as_an_unstable_link() {
    let policy = RecoveryPolicy {
        max_recoveries: 2,
        ..quick()
    };
    let (mut rig, mut dial) = connected(policy, config()).await;
    for _ in 0..2 {
        dial.service.close(1011, "internal").await;
        let (again, mut redial, _) = ready(&mut rig).await;
        assert!(matches!(again, Notice::Ready { after: Some(_), .. }));
        redial.service.send(handle("HANDLE-NEXT")).await;
        dial = redial;
    }
    dial.service.close(1011, "internal").await;
    assert_eq!(
        failure(&mut rig.0).await,
        Failure {
            error: Error::PeerClosed { code: Some(1011) },
            reason: FailureReason::Unstable,
            attempts: 0,
        }
    );
}

#[tokio::test]
async fn quota_refusal_and_local_faults_do_not_reconnect() {
    let (mut rig, mut dial) = connected(quick(), config()).await;
    dial.service
        .close(1011, "You exceeded your current quota")
        .await;
    assert_eq!(
        failure(&mut rig.0).await,
        Failure {
            error: Error::QuotaExceeded,
            reason: FailureReason::NotRecoverable,
            attempts: 0,
        }
    );
    assert!(
        timeout(Duration::from_millis(80), rig.1.recv())
            .await
            .is_err()
    );

    // Refused during a reconnect: no further attempts either.
    let (mut rig, mut dial) = connected(quick(), config()).await;
    dial.service.close(1008, "idle").await;
    let (supervisor, dials) = (&mut rig.0, &mut rig.1);
    let refuse = async {
        let mut redial = next_dial(dials).await;
        redial.service.receive().await;
        redial.service.close(4029, "usage limit reached").await;
        redial
    };
    let (ended, _redial) = tokio::join!(failure(supervisor), refuse);
    assert_eq!(ended.error, Error::QuotaExceeded);
    assert_eq!(ended.reason, FailureReason::NotRecoverable);
    assert_eq!(ended.attempts, 1);

    // A protocol fault is not a transport outage.
    let (mut rig, mut dial) = connected(quick(), config()).await;
    dial.service.send(json!({"toolCall":{}})).await;
    assert_eq!(
        failure(&mut rig.0).await,
        Failure {
            error: Error::UnsupportedMessage,
            reason: FailureReason::NotRecoverable,
            attempts: 0,
        }
    );
}

#[tokio::test]
async fn refused_handle_falls_back_to_a_fresh_session_only_when_policy_allows() {
    for require_context in [true, false] {
        let policy = RecoveryPolicy {
            require_context,
            ..quick()
        };
        let (mut rig, mut dial) = connected(policy, config()).await;
        dial.service.close(1008, "idle").await;
        let (supervisor, dials) = (&mut rig.0, &mut rig.1);
        let serve = async {
            let mut refused = next_dial(dials).await;
            assert!(refused.resumed);
            refused.service.receive().await;
            refused
                .service
                .send(json!({"error":{"code":400,"status":"INVALID_ARGUMENT"}}))
                .await;
            if require_context {
                return None;
            }
            let mut fresh = next_dial(dials).await;
            let setup = fresh.service.accept().await;
            Some((fresh, setup, refused))
        };
        let settle = async {
            loop {
                match raw(supervisor).await {
                    Ok(Notice::Unavailable { .. }) => {}
                    other => return other,
                }
            }
        };
        let (outcome, served) = tokio::join!(settle, serve);
        match (outcome, served) {
            (Err(ended), None) => {
                assert_eq!(ended.error, Error::ServerRejected);
                assert_eq!(ended.reason, FailureReason::NotRecoverable);
            }
            (
                Ok(Notice::Ready {
                    context, attempts, ..
                }),
                Some((fresh, setup, _)),
            ) => {
                assert_eq!(context, Context::Fresh);
                assert_eq!(attempts, 2);
                assert!(!fresh.resumed);
                assert!(setup["setup"].get("sessionResumption").is_none());
            }
            other => panic!("unexpected outcome: {:?}", other.0),
        }
    }
}

#[tokio::test]
async fn first_connection_is_never_retried_and_shutdown_is_terminal() {
    let (dialer, mut dials) = connector(config());
    let mut supervisor = Supervisor::new(dialer, quick()).unwrap();
    let refuse = async { drop(next_dial(&mut dials).await) };
    let (ended, ()) = tokio::join!(failure(&mut supervisor), refuse);
    assert_eq!(ended.reason, FailureReason::InitialConnect);
    assert!(
        timeout(Duration::from_millis(80), dials.recv())
            .await
            .is_err()
    );

    let (mut rig, _dial) = connected(quick(), config()).await;
    rig.0.shutdown();
    assert_eq!(
        failure(&mut rig.0).await,
        Failure {
            error: Error::Closed,
            reason: FailureReason::Shutdown,
            attempts: 0,
        }
    );
    assert_eq!(rig.0.try_start(request(1)), Err(Error::NotConnected));

    // Recovery disabled: today's behavior, one explicit terminal failure.
    let (mut rig, mut dial) = connected(RecoveryPolicy::DISABLED, config()).await;
    dial.service.close(1008, "idle").await;
    assert_eq!(
        failure(&mut rig.0).await,
        Failure {
            error: Error::PeerClosed { code: Some(1008) },
            reason: FailureReason::NotRecoverable,
            attempts: 0,
        }
    );
    assert!(
        Supervisor::new(
            connector(config()).0,
            RecoveryPolicy {
                max_attempts: 99,
                ..quick()
            }
        )
        .is_err()
    );
}

// ---- A failed connection that still holds an answer ----------------------

const RATE: u64 = crate::OUTPUT_RATE as u64;
const LARGEST: usize = crate::MAX_OUTPUT_SAMPLES;

/// Take audio at 24,000 samples per second until `stop` returns true for a
/// notice, collecting every non-audio notice and the samples played.
async fn play_until(
    rig: &mut Rig,
    mut stop: impl FnMut(&Notice) -> bool,
) -> (u64, Vec<&'static str>) {
    let mut samples = 0u64;
    let mut order = Vec::new();
    loop {
        let notice = timeout(Duration::from_secs(900), rig.0.next())
            .await
            .expect("a notice")
            .expect("no terminal failure");
        let done = stop(&notice);
        match notice {
            Notice::Event(Event::Audio { pcm, .. }) => {
                samples += pcm.len() as u64;
                tokio::time::sleep(Duration::from_micros(pcm.len() as u64 * 1_000_000 / RATE))
                    .await;
            }
            Notice::Event(Event::GenerationComplete { .. }) => order.push("generated"),
            Notice::Event(Event::TurnComplete { .. }) => order.push("complete"),
            Notice::Event(_) => {}
            Notice::Unavailable { .. } => order.push("unavailable"),
            Notice::Ready { .. } => order.push("ready"),
            Notice::Settled { .. } => order.push("settled"),
            Notice::TurnLost { .. } => order.push("lost"),
        }
        if done {
            return (samples, order);
        }
    }
}

#[tokio::test(start_paused = true)]
async fn buffered_answer_plays_out_after_its_connection_dies_while_a_replacement_is_made() {
    // This legacy consumer polls once per 2-second PCM message. Recovery now
    // counts from the actor's failure observation, not the later next() call;
    // use the production outage budget while testing playback/drain ordering.
    let policy = RecoveryPolicy {
        outage_budget: Duration::from_secs(15),
        ..quick()
    };
    let (mut rig, mut dial) = connected(policy, config()).await;
    speak(&mut rig, &mut dial, 1, true).await;
    // A minute of speech generated in one burst, then the connection dies:
    // the service's lifetime limit, a network change, an idle proxy.
    for _ in 0..30 {
        dial.service.send(audio(7, LARGEST)).await;
    }
    dial.service
        .send(json!({"serverContent":{"generationComplete":true}}))
        .await;
    dial.service.close(1011, "internal").await;
    let died = Instant::now();
    // The replacement is dialed and accepted while the answer is still playing.
    let (supervisor, dials) = (&mut rig.0, &mut rig.1);
    let replace = async {
        let mut redial = dial_within(dials, Duration::from_secs(30)).await;
        redial.service.accept().await;
        (redial, died.elapsed())
    };
    let play = async {
        let mut samples = 0u64;
        let mut order = Vec::new();
        loop {
            match timeout(Duration::from_secs(900), supervisor.next())
                .await
                .unwrap()
                .unwrap()
            {
                Notice::Event(Event::Audio { pcm, .. }) => {
                    assert!(pcm.iter().all(|sample| *sample == 7));
                    samples += pcm.len() as u64;
                    tokio::time::sleep(Duration::from_micros(pcm.len() as u64 * 1_000_000 / RATE))
                        .await;
                }
                Notice::Event(Event::GenerationComplete { .. }) => {
                    order.push(("generated", died.elapsed()))
                }
                Notice::Unavailable { error } => {
                    assert_eq!(error, Error::PeerClosed { code: Some(1011) });
                    order.push(("unavailable", died.elapsed()));
                }
                Notice::Ready { context, after, .. } => {
                    assert_eq!(context, Context::Resumed);
                    assert!(after.is_some());
                    order.push(("ready", died.elapsed()));
                }
                Notice::Settled { lineage, .. } => {
                    assert_eq!(lineage.request.get(), 1);
                    order.push(("settled", died.elapsed()));
                    return (samples, order);
                }
                other => panic!("unexpected notice: {other:?}"),
            }
        }
    };
    let ((samples, order), (_redial, replaced_in)) = tokio::join!(play, replace);
    // The whole answer, at speaking speed, from a connection that was gone.
    assert_eq!(samples, 60 * RATE);
    let at = |name: &str| order.iter().find(|(kind, _)| *kind == name).unwrap().1;
    // Without upkeep the failure is noticed when the caller next asks: here
    // once per two-second message. The replacement follows at once.
    assert!(at("unavailable") <= Duration::from_secs(4), "{order:?}");
    assert!(at("ready") <= Duration::from_secs(6), "{order:?}");
    assert!(replaced_in <= Duration::from_secs(6));
    assert!(at("settled") >= Duration::from_secs(59), "{order:?}");
    assert!(at("generated") <= at("settled"));
    assert!(rig.0.is_connected());
}

#[tokio::test(start_paused = true)]
async fn follow_up_during_a_dead_connections_answer_is_served_by_the_replacement() {
    // This legacy consumer polls once per 2-second PCM message. Recovery now
    // counts from the actor's failure observation, not the later next() call;
    // use the production outage budget while testing playback/drain ordering.
    let policy = RecoveryPolicy {
        outage_budget: Duration::from_secs(15),
        ..quick()
    };
    let (mut rig, mut dial) = connected(policy, config()).await;
    speak(&mut rig, &mut dial, 1, true).await;
    for _ in 0..30 {
        dial.service.send(audio(7, LARGEST)).await;
    }
    dial.service
        .send(json!({"serverContent":{"generationComplete":true}}))
        .await;
    dial.service.close(1011, "internal").await;
    let (supervisor, dials) = (&mut rig.0, &mut rig.1);
    let replace = async {
        let mut redial = dial_within(dials, Duration::from_secs(30)).await;
        redial.service.accept().await;
        redial
    };
    // Ten seconds of the old answer are heard before the person speaks again.
    let listen = async {
        let mut samples = 0u64;
        let mut ready = false;
        while samples < 10 * RATE || !ready {
            match timeout(Duration::from_secs(900), supervisor.next())
                .await
                .unwrap()
                .unwrap()
            {
                Notice::Event(Event::Audio { pcm, .. }) => {
                    samples += pcm.len() as u64;
                    tokio::time::sleep(Duration::from_micros(pcm.len() as u64 * 1_000_000 / RATE))
                        .await;
                }
                Notice::Ready { .. } => ready = true,
                _ => {}
            }
        }
        samples
    };
    let (heard, mut redial) = tokio::join!(listen, replace);
    assert!(heard < 14 * RATE);
    // The follow-up goes to the live connection. The dead one's remaining
    // fifty seconds are cancelled with the request they belonged to.
    rig.0.retire(request(1));
    rig.0.try_start(request(2)).unwrap();
    rig.0
        .try_audio(request(2), 0, std::time::Instant::now(), &[2; 160])
        .unwrap();
    rig.0.try_end(request(2)).unwrap();
    for field in ["activityStart", "audio", "activityEnd"] {
        assert!(
            redial.service.receive().await["realtimeInput"]
                .get(field)
                .is_some()
        );
    }
    redial.service.send(audio(9, 240)).await;
    redial.service.send(idle()).await;
    let asked = Instant::now();
    let (_, order) = play_until(&mut rig, |notice| {
        matches!(notice, Notice::Event(Event::TurnComplete { lineage, .. }) if lineage.request.get() == 2)
    })
    .await;
    // No stale audio was played on the way. The cancelled answer's own
    // lifecycle may still be reported, but it is neither settled nor lost:
    // it was cancelled.
    assert!(
        asked.elapsed() < Duration::from_secs(1),
        "{:?}",
        asked.elapsed()
    );
    assert_eq!(order.last(), Some(&"complete"));
    assert!(
        !order.contains(&"settled") && !order.contains(&"lost"),
        "{order:?}"
    );
}

#[tokio::test(start_paused = true)]
async fn answer_cut_before_generation_completed_is_delivered_as_far_as_it_got_then_reported_lost() {
    // This legacy consumer polls once per 2-second PCM message. Recovery now
    // counts from the actor's failure observation, not the later next() call;
    // use the production outage budget while testing playback/drain ordering.
    let policy = RecoveryPolicy {
        outage_budget: Duration::from_secs(15),
        ..quick()
    };
    let (mut rig, mut dial) = connected(policy, config()).await;
    speak(&mut rig, &mut dial, 1, true).await;
    for _ in 0..5 {
        dial.service.send(audio(7, LARGEST)).await;
    }
    dial.service.close(1011, "internal").await;
    let (supervisor, dials) = (&mut rig.0, &mut rig.1);
    let replace = async {
        let mut redial = dial_within(dials, Duration::from_secs(30)).await;
        redial.service.accept().await;
        redial
    };
    let play = async {
        let mut samples = 0u64;
        loop {
            match timeout(Duration::from_secs(900), supervisor.next())
                .await
                .unwrap()
                .unwrap()
            {
                Notice::Event(Event::Audio { pcm, .. }) => {
                    samples += pcm.len() as u64;
                    tokio::time::sleep(Duration::from_micros(pcm.len() as u64 * 1_000_000 / RATE))
                        .await;
                }
                Notice::TurnLost {
                    lineage,
                    stage,
                    error,
                } => {
                    assert_eq!(lineage.request.get(), 1);
                    assert_eq!(stage, Stage::Responding);
                    assert_eq!(error, Error::PeerClosed { code: Some(1011) });
                    return samples;
                }
                Notice::Settled { .. } => panic!("an incomplete answer is not settled"),
                _ => {}
            }
        }
    };
    let (samples, _redial) = tokio::join!(play, replace);
    // Everything that arrived was played; the loss is reported only after it.
    assert_eq!(samples, 10 * RATE);
}

#[tokio::test(start_paused = true)]
async fn unrecoverable_failure_still_delivers_the_buffered_answer_before_ending() {
    let (mut rig, mut dial) = connected(quick(), config()).await;
    speak(&mut rig, &mut dial, 1, true).await;
    for _ in 0..10 {
        dial.service.send(audio(7, LARGEST)).await;
    }
    dial.service
        .send(json!({"serverContent":{"generationComplete":true}}))
        .await;
    dial.service
        .close(1011, "You exceeded your current quota")
        .await;
    let (samples, order) =
        play_until(&mut rig, |notice| matches!(notice, Notice::Settled { .. })).await;
    assert_eq!(samples, 20 * RATE);
    assert_eq!(order.first(), Some(&"unavailable"));
    assert_eq!(order.last(), Some(&"settled"));
    assert_eq!(
        failure(&mut rig.0).await,
        Failure {
            error: Error::QuotaExceeded,
            reason: FailureReason::NotRecoverable,
            attempts: 0,
        }
    );
    // No reconnect was attempted for a quota refusal.
    assert!(timeout(Duration::from_secs(5), rig.1.recv()).await.is_err());
}

#[tokio::test(start_paused = true)]
async fn upkeep_notices_a_dead_connection_and_redials_without_being_asked_for_notices() {
    let (mut rig, mut dial) = connected(quick(), config()).await;
    speak(&mut rig, &mut dial, 1, true).await;
    for _ in 0..30 {
        dial.service.send(audio(7, LARGEST)).await;
    }
    dial.service
        .send(json!({"serverContent":{"generationComplete":true}}))
        .await;
    dial.service.close(1011, "internal").await;
    let died = Instant::now();
    // The caller is busy playing and asks for nothing. Its service tick alone
    // must be enough to notice the failure and start the replacement.
    let redial = loop {
        rig.0.maintain();
        if let Ok(redial) = rig.1.try_recv() {
            break redial;
        }
        assert!(
            died.elapsed() < Duration::from_secs(1),
            "no redial from upkeep"
        );
        tokio::time::sleep(Duration::from_millis(2)).await;
    };
    assert!(
        died.elapsed() <= Duration::from_millis(50),
        "{:?}",
        died.elapsed()
    );
    assert!(redial.resumed);
    assert!(!rig.0.is_connected());
    assert_eq!(rig.0.try_start(request(2)), Err(Error::NotConnected));
    // Backoff and the outage budget are applied by upkeep too.
    drop(redial);
    let second = loop {
        rig.0.maintain();
        // The failed attempt is observed through `next`, which never blocks
        // the tick: it is polled and abandoned.
        let _ = timeout(Duration::from_millis(1), rig.0.next()).await;
        if let Ok(redial) = rig.1.try_recv() {
            break redial;
        }
        assert!(died.elapsed() < Duration::from_secs(5), "no second attempt");
        tokio::time::sleep(Duration::from_millis(2)).await;
    };
    assert!(second.resumed);
}
