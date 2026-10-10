//! Recovery control must advance without consuming buffered answer PCM.
use super::*;

#[tokio::test(start_paused = true)]
async fn upkeep_harvests_completed_redial_without_consuming_buffered_pcm() {
    let (mut rig, mut first) = connected(quick(), config()).await;
    speak(&mut rig, &mut first, 1, true).await;
    first.service.send(audio(7, 240)).await;
    first.service.close(1011, "outage").await;
    let Link::Connected(live) = &mut rig.0.link else {
        panic!("live")
    };
    live.state
        .wait_for(|state| *state != State::Ready)
        .await
        .unwrap();
    rig.0.maintain();
    assert!(matches!(
        raw(&mut rig.0).await,
        Ok(Notice::Unavailable { .. })
    ));
    let mut replacement = next_dial(&mut rig.1).await;
    replacement.service.accept().await;
    for _ in 0..32 {
        tokio::task::yield_now().await;
        rig.0.maintain();
        if rig.0.is_connected() {
            break;
        }
    }
    assert!(
        rig.0.is_connected(),
        "completed setup remained unharvested without next()"
    );
    let mut samples = Vec::new();
    for _ in 0..8 {
        match raw(&mut rig.0).await.unwrap() {
            Notice::Event(Event::Audio { pcm, .. }) => samples.extend(pcm),
            Notice::TurnLost { .. } => break,
            Notice::Ready { .. } | Notice::Event(_) => {}
            other => panic!("unexpected {other:?}"),
        }
    }
    assert_eq!(samples, vec![7; 240], "control progress consumed PCM");
    rig.0.shutdown();
}

async fn failed_source(supervisor: &mut Supervisor<FakeConnector>) -> Instant {
    let Link::Connected(live) = &mut supervisor.link else {
        panic!("connected")
    };
    timeout(WAIT, live.state.wait_for(|state| *state != State::Ready))
        .await
        .unwrap()
        .unwrap();
    live.connection.observed_state().1
}

async fn finished_attempt(supervisor: &Supervisor<FakeConnector>) {
    for _ in 0..128 {
        if matches!(&supervisor.link, Link::Connecting { attempt, .. } if attempt.0.is_finished()) {
            return;
        }
        tokio::task::yield_now().await;
    }
    panic!("attempt did not finish");
}

async fn update(supervisor: &mut Supervisor<FakeConnector>) -> LinkUpdate {
    for _ in 0..128 {
        if let Some(update) = supervisor.poll_link() {
            return update;
        }
        tokio::task::yield_now().await;
    }
    panic!("expected link update");
}

async fn unread(generated: bool, policy: RecoveryPolicy) -> (Rig, Instant, Vec<i16>) {
    let (mut rig, mut first) = connected(policy, config()).await;
    speak(&mut rig, &mut first, 1, true).await;
    let mut expected = Vec::new();
    for value in [3, 7, 11, 19] {
        first.service.send(audio(value, 240)).await;
        expected.extend(std::iter::repeat_n(value, 240));
    }
    if generated {
        first
            .service
            .send(json!({"serverContent":{"generationComplete":true}}))
            .await;
    }
    first.service.close(1011, "recoverable loss").await;
    let observed = failed_source(&mut rig.0).await;
    (rig, observed, expected)
}

async fn replacement_without_output(rig: &mut Rig) -> (LinkUpdate, Dial) {
    let unavailable = update(&mut rig.0).await;
    assert!(matches!(
        unavailable.kind,
        LinkUpdateKind::Unavailable { .. }
    ));
    let mut dial = next_dial(&mut rig.1).await;
    dial.service.accept().await;
    finished_attempt(&rig.0).await;
    let ready = update(&mut rig.0).await;
    assert!(matches!(ready.kind, LinkUpdateKind::Ready { .. }));
    assert!(ready.revision > unavailable.revision);
    assert!(ready.session > unavailable.session);
    (ready, dial)
}

#[tokio::test(start_paused = true)]
async fn split_poll_recovers_with_every_original_pcm_block_unconsumed() {
    let (mut rig, failed_at, expected) = unread(true, quick()).await;
    tokio::time::advance(Duration::from_millis(100)).await;
    let unavailable = update(&mut rig.0).await;
    assert_eq!(
        unavailable.observed_at, failed_at,
        "failure was restamped by polling"
    );
    let mut replacement = next_dial(&mut rig.1).await;
    replacement.service.accept().await;
    finished_attempt(&rig.0).await;
    let ready = update(&mut rig.0).await;
    assert!(rig.0.is_connected());
    let LinkUpdateKind::Ready {
        outage_started_at,
        completed_at,
        ..
    } = ready.kind
    else {
        panic!("ready")
    };
    assert_eq!(outage_started_at, Some(failed_at));
    assert_eq!(ready.observed_at, completed_at);
    assert!(completed_at >= failed_at);
    let mut actual = Vec::new();
    loop {
        match timeout(WAIT, rig.0.next_output()).await.unwrap().unwrap() {
            OutputNotice::Event(Event::Audio { lineage, pcm, .. }) => {
                assert_eq!(lineage.session.get(), 1);
                actual.extend(pcm);
            }
            OutputNotice::Event(Event::GenerationComplete { .. }) => {}
            OutputNotice::Settled { lineage, .. } => {
                assert_eq!(lineage.request.get(), 1);
                assert_eq!(actual, expected);
                break;
            }
            other => panic!("unexpected {other:?}"),
        }
    }
    assert!(
        replacement
            .service
            .try_receive(Duration::from_millis(5))
            .await
            .is_none(),
        "replayed input"
    );
    assert!(rig.0.poll_link().is_none());
    rig.0.shutdown();
}

#[tokio::test(start_paused = true)]
async fn stalled_link_consumer_retains_one_original_update_and_one_drain() {
    let (mut rig, failed_at, expected) = unread(true, quick()).await;
    rig.0.maintain();
    let first = rig.0.pending_link.unwrap();
    assert_eq!(first.observed_at, failed_at);
    let mut replacement = next_dial(&mut rig.1).await;
    replacement.service.accept().await;
    finished_attempt(&rig.0).await;
    replacement
        .service
        .close(1011, "You exceeded your current quota")
        .await;
    for _ in 0..128 {
        tokio::task::yield_now().await;
        rig.0.maintain();
        assert_eq!(rig.0.pending_link, Some(first));
        assert!(rig.0.draining.is_some());
        assert!(rig.0.pending_output.is_none());
    }
    assert_eq!(rig.0.poll_link(), Some(first));
    let second = update(&mut rig.0).await;
    assert!(matches!(
        second.kind,
        LinkUpdateKind::Unavailable {
            error: Error::QuotaExceeded
        }
    ));
    assert_eq!(second.session.get(), 2);
    assert!(second.revision > first.revision);
    let terminal = update(&mut rig.0).await;
    assert!(matches!(
        terminal.kind,
        LinkUpdateKind::Terminal {
            failure: Failure {
                error: Error::QuotaExceeded,
                ..
            }
        }
    ));
    assert!(terminal.revision > second.revision);
    assert!(
        rig.1.try_recv().is_err(),
        "fatal replacement caused another dial"
    );
    let mut actual = Vec::new();
    let mut settled = 0;
    loop {
        match timeout(WAIT, rig.0.next_output()).await.unwrap() {
            Ok(OutputNotice::Event(Event::Audio { pcm, .. })) => actual.extend(pcm),
            Ok(OutputNotice::Event(Event::GenerationComplete { .. })) => {}
            Ok(OutputNotice::Settled { .. }) => {
                assert_eq!(actual, expected);
                settled += 1;
            }
            Err(failure) => {
                assert_eq!(failure.error, Error::QuotaExceeded);
                break;
            }
            other => panic!("unexpected {other:?}"),
        }
    }
    assert_eq!(settled, 1);
    assert!(rig.0.next_output().await.is_err());
}

#[tokio::test(start_paused = true)]
async fn failed_attempt_backoff_uses_original_completion_not_harvest_time() {
    let (mut rig, _, _) = unread(false, quick()).await;
    assert!(matches!(
        update(&mut rig.0).await.kind,
        LinkUpdateKind::Unavailable { .. }
    ));
    let mut second = next_dial(&mut rig.1).await;
    second.service.receive().await;
    second.service.close(1011, "setup failed").await;
    finished_attempt(&rig.0).await;
    let finished_before = Instant::now();
    tokio::time::advance(Duration::from_millis(100)).await;
    assert!(rig.0.poll_link().is_none()); // Harvest failure, start bounded backoff.
    let Link::Waiting { until, .. } = rig.0.link else {
        panic!("backoff")
    };
    assert!(until <= finished_before + quick().first_backoff);
    assert!(until < Instant::now());
    assert!(rig.0.poll_link().is_none()); // Already-due backoff starts the next dial.
    let third = next_dial(&mut rig.1).await;
    assert_eq!(third.session.get(), 3);
    rig.0.shutdown();
}

#[tokio::test(start_paused = true)]
async fn setup_completed_inside_budget_keeps_source_time_when_harvested_late() {
    let policy = RecoveryPolicy {
        outage_budget: Duration::from_millis(200),
        ..quick()
    };
    let (mut rig, failed_at, _) = unread(false, policy).await;
    update(&mut rig.0).await;
    let mut replacement = next_dial(&mut rig.1).await;
    replacement.service.accept().await;
    finished_attempt(&rig.0).await;
    let completed_before = Instant::now();
    tokio::time::advance(Duration::from_millis(300)).await;
    let recovered = update(&mut rig.0).await;
    let LinkUpdateKind::Ready {
        completed_at,
        outage_started_at,
        ..
    } = recovered.kind
    else {
        panic!("within-budget setup must survive late polling")
    };
    assert!(completed_at <= completed_before);
    assert_eq!(outage_started_at, Some(failed_at));
    assert!(completed_at < failed_at + policy.outage_budget);
    assert!(rig.0.is_connected());
    rig.0.shutdown();
}

#[tokio::test(start_paused = true)]
async fn setup_completed_after_original_budget_cannot_win_by_harvest_order() {
    let policy = RecoveryPolicy {
        outage_budget: Duration::from_millis(200),
        ..quick()
    };
    let (mut rig, failed_at, _) = unread(false, policy).await;
    update(&mut rig.0).await;
    let mut replacement = next_dial(&mut rig.1).await;
    tokio::time::advance(Duration::from_millis(250)).await;
    replacement.service.accept().await;
    finished_attempt(&rig.0).await;
    let terminal = update(&mut rig.0).await;
    assert_eq!(terminal.observed_at, Instant::now());
    assert!(terminal.observed_at >= failed_at + policy.outage_budget);
    assert!(matches!(
        terminal.kind,
        LinkUpdateKind::Terminal {
            failure: Failure {
                reason: FailureReason::BudgetExhausted,
                ..
            }
        }
    ));
    assert!(!rig.0.is_connected());
    rig.0.shutdown();
}

#[tokio::test(start_paused = true)]
async fn late_first_outage_poll_does_not_grant_a_fresh_budget_or_dial() {
    let policy = RecoveryPolicy {
        outage_budget: Duration::from_millis(100),
        ..quick()
    };
    let (mut rig, failed_at, _) = unread(false, policy).await;
    tokio::time::advance(Duration::from_millis(150)).await;
    let unavailable = update(&mut rig.0).await;
    assert_eq!(unavailable.observed_at, failed_at);
    assert!(matches!(
        unavailable.kind,
        LinkUpdateKind::Unavailable { .. }
    ));
    let terminal = update(&mut rig.0).await;
    assert_eq!(terminal.observed_at, Instant::now());
    assert!(terminal.observed_at >= failed_at + policy.outage_budget);
    assert!(matches!(
        terminal.kind,
        LinkUpdateKind::Terminal {
            failure: Failure {
                reason: FailureReason::BudgetExhausted,
                attempts: 0,
                ..
            }
        }
    ));
    assert!(rig.1.try_recv().is_err());
    rig.0.shutdown();
}

#[tokio::test(start_paused = true)]
async fn expired_backoff_does_not_start_an_attempt_after_the_outage_budget() {
    let policy = RecoveryPolicy {
        outage_budget: Duration::from_millis(200),
        ..quick()
    };
    let (mut rig, failed_at, _) = unread(false, policy).await;
    update(&mut rig.0).await;
    let mut second = next_dial(&mut rig.1).await;
    second.service.receive().await;
    second.service.close(1011, "setup failed").await;
    finished_attempt(&rig.0).await;
    assert!(rig.0.poll_link().is_none()); // Waiting.
    tokio::time::advance(Duration::from_millis(250)).await;
    let terminal = update(&mut rig.0).await;
    assert_eq!(terminal.observed_at, Instant::now());
    assert!(terminal.observed_at >= failed_at + policy.outage_budget);
    assert!(matches!(
        terminal.kind,
        LinkUpdateKind::Terminal {
            failure: Failure {
                reason: FailureReason::BudgetExhausted,
                ..
            }
        }
    ));
    assert!(rig.1.try_recv().is_err(), "out-of-budget dial started");
    rig.0.shutdown();
}

#[tokio::test(start_paused = true)]
async fn setup_then_close_before_harvest_never_reports_transient_ready() {
    let (mut rig, _, _) = unread(false, quick()).await;
    update(&mut rig.0).await;
    let mut second = next_dial(&mut rig.1).await;
    second.service.accept().await;
    finished_attempt(&rig.0).await;
    second
        .service
        .close(1011, "You exceeded your current quota")
        .await;
    for _ in 0..32 {
        tokio::task::yield_now().await;
    }
    let observed = update(&mut rig.0).await;
    assert!(matches!(
        observed.kind,
        LinkUpdateKind::Unavailable {
            error: Error::QuotaExceeded
        }
    ));
    assert_eq!(observed.session.get(), 2);
    assert!(!rig.0.is_connected());
    rig.0.shutdown();
}

#[tokio::test(start_paused = true)]
async fn pending_ready_is_superseded_if_connection_dies_before_consumer_polls() {
    let (mut rig, _, _) = unread(false, quick()).await;
    update(&mut rig.0).await;
    let mut second = next_dial(&mut rig.1).await;
    second.service.accept().await;
    finished_attempt(&rig.0).await;
    rig.0.maintain();
    let ready = rig.0.pending_link.unwrap();
    assert!(matches!(ready.kind, LinkUpdateKind::Ready { .. }));
    second
        .service
        .close(1011, "You exceeded your current quota")
        .await;
    let failed_at = failed_source(&mut rig.0).await;
    let unavailable = update(&mut rig.0).await;
    assert_eq!(unavailable.observed_at, failed_at);
    assert!(unavailable.revision > ready.revision);
    assert!(matches!(
        unavailable.kind,
        LinkUpdateKind::Unavailable { .. }
    ));
    assert!(!rig.0.is_connected());
    rig.0.shutdown();
}

#[tokio::test(start_paused = true)]
async fn local_retirement_and_shutdown_do_not_wait_for_pending_control_or_data() {
    let (mut rig, _, _) = unread(true, quick()).await;
    rig.0.maintain();
    let pending = rig.0.pending_link.unwrap();
    rig.0.retire(request(1));
    assert!(rig.0.draining.as_ref().unwrap().tracked.get().is_none());
    assert_eq!(rig.0.pending_link, Some(pending));
    let mut second = next_dial(&mut rig.1).await;
    second.service.accept().await;
    finished_attempt(&rig.0).await;
    rig.0.shutdown();
    assert!(rig.0.draining.is_none());
    assert!(rig.0.pending_output.is_none());
    let now = Instant::now();
    assert_eq!(
        rig.0.next_output().await.unwrap_err().reason,
        FailureReason::Shutdown
    );
    assert_eq!(Instant::now(), now, "stop waited for control consumer");
    let stopped = rig.0.poll_link().unwrap();
    assert!(matches!(
        stopped.kind,
        LinkUpdateKind::Terminal {
            failure: Failure {
                reason: FailureReason::Shutdown,
                ..
            }
        }
    ));
    assert!(rig.0.poll_link().is_none());
}

#[tokio::test(start_paused = true)]
async fn replaced_input_query_is_exact_read_only_and_never_grants_input() {
    let (mut rig, mut first) = connected(quick(), config()).await;
    assert!(!rig.0.input_belongs_to_replaced_connection(request(1)));
    speak(&mut rig, &mut first, 1, false).await;
    assert!(!rig.0.input_belongs_to_replaced_connection(request(1)));
    first.service.close(1011, "outage").await;
    failed_source(&mut rig.0).await;
    assert!(!rig.0.input_belongs_to_replaced_connection(request(1)));
    let (_, mut second) = replacement_without_output(&mut rig).await;
    rig.0.retire(request(1));
    assert!(
        rig.0.input_belongs_to_replaced_connection(request(1)),
        "retirement must retain submitted lineage"
    );
    assert!(!rig.0.input_belongs_to_replaced_connection(request(2)));
    assert_eq!(rig.0.try_end(request(1)), Err(Error::StaleRequest));
    assert_eq!(
        rig.0
            .try_audio(request(1), 0, std::time::Instant::now(), &[1; 160]),
        Err(Error::StaleRequest)
    );
    assert!(
        second
            .service
            .try_receive(Duration::from_millis(5))
            .await
            .is_none()
    );
    rig.0.try_start(request(2)).unwrap();
    assert!(!rig.0.input_belongs_to_replaced_connection(request(1)));
    assert!(!rig.0.input_belongs_to_replaced_connection(request(2)));
    rig.0.shutdown();
    assert!(!rig.0.input_belongs_to_replaced_connection(request(1)));
}

#[tokio::test(start_paused = true)]
async fn link_revision_exhaustion_refuses_recovery_without_reusing_an_identity() {
    let (mut rig, _, expected) = unread(false, quick()).await;
    rig.0.revision = u64::MAX;
    assert!(rig.0.poll_link().is_none());
    assert!(!rig.0.is_connected());
    assert!(rig.1.try_recv().is_err());
    let mut actual = Vec::new();
    let mut lost = 0;
    loop {
        match rig.0.next_output().await {
            Ok(OutputNotice::Event(Event::Audio { pcm, .. })) => actual.extend(pcm),
            Ok(OutputNotice::TurnLost { .. }) => {
                lost += 1;
                assert_eq!(actual, expected);
            }
            Err(failure) => {
                assert_eq!(failure.reason, FailureReason::Lifecycle);
                break;
            }
            other => panic!("unexpected {other:?}"),
        }
    }
    assert_eq!(lost, 1);
    assert_eq!(rig.0.revision, u64::MAX);
}

#[tokio::test(start_paused = true)]
async fn session_identity_exhaustion_never_starts_a_wrapped_dial() {
    let (mut rig, _) = connected(quick(), config()).await;
    rig.0.sessions = u64::MAX;
    rig.0.dial(1, None);
    let update = update(&mut rig.0).await;
    assert_eq!(update.session.get(), u64::MAX);
    assert!(matches!(
        update.kind,
        LinkUpdateKind::Terminal {
            failure: Failure {
                reason: FailureReason::Lifecycle,
                ..
            }
        }
    ));
    assert!(rig.1.try_recv().is_err());
    assert!(!rig.0.is_connected());
}
