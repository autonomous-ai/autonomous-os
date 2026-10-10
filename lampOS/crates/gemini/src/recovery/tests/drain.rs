//! A replacement link must not consume the single outstanding drain slot.
use super::*;

const CHUNKS: usize = 4;
const SAMPLES: usize = 240;

async fn observe_link_failure(rig: &mut Rig) {
    let Link::Connected(live) = &mut rig.0.link else {
        panic!("connected link before remote failure");
    };
    timeout(WAIT, live.state.wait_for(|state| *state != State::Ready))
        .await
        .expect("actor reports remote failure")
        .expect("state channel remains available");
    rig.0.maintain();
}

/// Drive only connection setup, leaving the old answer completely unread.
/// This uses the production progress/apply path and actual in-memory transport.
async fn accept_replacement(rig: &mut Rig) -> Dial {
    while rig.0.poll_link().is_some() {}
    let mut dial = next_dial(&mut rig.1).await;
    let policy = rig.0.policy;
    let (step, _) = tokio::join!(progress(&mut rig.0.link, &policy), dial.service.accept());
    rig.0.apply(step);
    assert!(matches!(
        rig.0.poll_link(),
        Some(LinkUpdate { session, kind: LinkUpdateKind::Ready { after: Some(_), .. }, .. }) if session == dial.session
    ));
    dial
}

async fn with_unread_answer(generated: bool) -> (Rig, Dial) {
    let (mut rig, mut original) = connected(quick(), config()).await;
    speak(&mut rig, &mut original, 1, true).await;
    for index in 0..CHUNKS {
        original
            .service
            .send(audio(7 + index as i16, SAMPLES))
            .await;
    }
    if generated {
        original
            .service
            .send(json!({"serverContent":{"generationComplete":true}}))
            .await;
    }
    original.service.close(1011, "original loss").await;
    observe_link_failure(&mut rig).await;
    let replacement = accept_replacement(&mut rig).await;
    assert_eq!(replacement.session.get(), 2);
    assert!(rig.0.draining.as_ref().unwrap().tracked.get().is_some());
    (rig, replacement)
}

fn audio_samples(notice: Notice, output: &mut Vec<i16>, terminal: &mut Vec<&'static str>) {
    match notice {
        Notice::Event(Event::Audio { lineage, pcm, .. }) => {
            assert_eq!(lineage.request, request(1));
            assert_eq!(lineage.session.get(), 1);
            output.extend(pcm);
        }
        Notice::Settled { lineage, .. } => {
            assert_eq!(lineage.request, request(1));
            assert_eq!(lineage.session.get(), 1);
            terminal.push("settled");
        }
        Notice::TurnLost { lineage, stage, .. } => {
            assert_eq!(lineage.request, request(1));
            assert_eq!(stage, Stage::Responding);
            terminal.push("lost");
        }
        Notice::Event(Event::GenerationComplete { .. }) | Notice::Unavailable { .. } => {}
        other => panic!("unexpected drain notice {other:?}"),
    }
}

fn expected_pcm() -> Vec<i16> {
    (0..CHUNKS)
        .flat_map(|index| std::iter::repeat_n(7 + index as i16, SAMPLES))
        .collect()
}

#[tokio::test(start_paused = true)]
async fn replacement_failure_preserves_buffered_answer_and_exact_terminal_outcome() {
    for generated in [false, true] {
        let (mut rig, mut replacement) = with_unread_answer(generated).await;
        // B never accepted input. Its fatal failure must not erase A's answer,
        // or report A complete before the actual received PCM is delivered.
        replacement
            .service
            .close(1011, "You exceeded your current quota")
            .await;
        observe_link_failure(&mut rig).await;
        let mut output = Vec::new();
        let mut terminal = Vec::new();
        loop {
            match raw(&mut rig.0).await {
                Ok(notice) => audio_samples(notice, &mut output, &mut terminal),
                Err(failure) => {
                    assert_eq!(failure.error, Error::QuotaExceeded);
                    assert_eq!(failure.reason, FailureReason::NotRecoverable);
                    break;
                }
            }
        }
        assert_eq!(output, expected_pcm(), "replacement erased accepted PCM");
        assert_eq!(terminal, [if generated { "settled" } else { "lost" }]);
        // The failure remains terminal, without a duplicated turn outcome.
        assert!(raw(&mut rig.0).await.is_err());
    }
}

#[tokio::test(start_paused = true)]
async fn another_reconnect_keeps_the_original_drain_without_replaying_input() {
    let (mut rig, mut replacement) = with_unread_answer(true).await;
    assert!(
        replacement
            .service
            .try_receive(Duration::from_millis(20))
            .await
            .is_none()
    );
    replacement.service.close(1011, "replacement loss").await;
    observe_link_failure(&mut rig).await;
    let mut third = accept_replacement(&mut rig).await;
    assert_eq!(third.session.get(), 3);
    assert!(
        third
            .service
            .try_receive(Duration::from_millis(20))
            .await
            .is_none(),
        "recovery must not replay old input"
    );
    let mut output = Vec::new();
    let mut terminal = Vec::new();
    for _ in 0..CHUNKS + 4 {
        audio_samples(
            raw(&mut rig.0).await.expect("third connection stays ready"),
            &mut output,
            &mut terminal,
        );
        if !terminal.is_empty() {
            break;
        }
    }
    assert_eq!(output, expected_pcm());
    assert_eq!(terminal, ["settled"]);
    assert!(rig.0.is_connected());
    rig.0.shutdown();
    assert_eq!(failure(&mut rig.0).await.reason, FailureReason::Shutdown);
}

#[tokio::test(start_paused = true)]
async fn replacement_rejects_reused_request_id_before_the_original_drain_settles() {
    let (rig, _replacement) = with_unread_answer(true).await;
    assert_eq!(
        rig.0.try_start(request(1)),
        Err(Error::StaleRequest),
        "request IDs must remain monotonic across connections"
    );
    assert!(rig.0.tracked.get().is_none());
    assert_eq!(
        rig.0
            .draining
            .as_ref()
            .unwrap()
            .tracked
            .get()
            .unwrap()
            .lineage
            .request,
        request(1),
        "a rejected start must not retire the pending answer"
    );
}

#[tokio::test(start_paused = true)]
async fn accepted_successor_retires_old_connection_output_without_an_extra_retire_call() {
    let (mut rig, mut replacement) = with_unread_answer(true).await;
    rig.0.try_start(request(2)).unwrap();
    rig.0
        .try_audio(request(2), 0, std::time::Instant::now(), &[2; 160])
        .unwrap();
    rig.0.try_end(request(2)).unwrap();
    for field in ["activityStart", "audio", "activityEnd"] {
        assert!(
            replacement.service.receive().await["realtimeInput"]
                .get(field)
                .is_some()
        );
    }
    replacement.service.send(audio(23, SAMPLES)).await;
    replacement.service.send(idle()).await;
    let mut received = 0;
    let mut completed = 0;
    for _ in 0..CHUNKS + 8 {
        match raw(&mut rig.0).await.unwrap() {
            Notice::Event(Event::Audio { lineage, pcm, .. }) => {
                assert_eq!(
                    lineage.request,
                    request(2),
                    "cancelled output escaped the drain"
                );
                assert!(pcm.iter().all(|value| *value == 23));
                received += pcm.len();
            }
            Notice::Event(Event::TurnComplete {
                lineage,
                idle: true,
            }) => {
                assert_eq!(lineage.request, request(2));
                completed += 1;
                break;
            }
            Notice::Settled { .. } | Notice::TurnLost { .. } => {
                panic!("locally superseded input must not later settle or be reported lost");
            }
            Notice::Unavailable { .. } | Notice::Event(_) => {}
            other => panic!("unexpected successor notice {other:?}"),
        }
    }
    assert_eq!(received, SAMPLES);
    assert_eq!(completed, 1);
}

#[tokio::test(start_paused = true)]
async fn audio_from_the_draining_request_cannot_enter_its_replacement() {
    let (rig, mut replacement) = with_unread_answer(true).await;
    assert_eq!(
        rig.0
            .try_audio(request(1), 1, std::time::Instant::now(), &[19; 160]),
        Err(Error::StaleRequest)
    );
    assert!(
        replacement
            .service
            .try_receive(Duration::from_millis(20))
            .await
            .is_none()
    );
    assert!(
        rig.0.is_connected(),
        "stale input must not kill the healthy replacement"
    );
}

#[tokio::test(start_paused = true)]
async fn end_from_the_draining_request_cannot_enter_its_replacement() {
    let (rig, mut replacement) = with_unread_answer(true).await;
    assert_eq!(rig.0.try_end(request(1)), Err(Error::StaleRequest));
    assert!(
        replacement
            .service
            .try_receive(Duration::from_millis(20))
            .await
            .is_none()
    );
    assert!(
        rig.0.is_connected(),
        "stale End must not kill the healthy replacement"
    );
}

#[tokio::test(start_paused = true)]
async fn same_session_retirement_can_still_close_submitted_input() {
    let (mut rig, mut dial) = connected(quick(), config()).await;
    speak(&mut rig, &mut dial, 1, false).await;
    rig.0.retire(request(1));
    assert!(rig.0.tracked.get().is_none());
    rig.0.try_end(request(1)).unwrap();
    // A retired open input closes locally without requesting an unowned reply.
    assert!(
        dial.service
            .try_receive(Duration::from_millis(20))
            .await
            .is_none()
    );
    rig.0.try_start(request(2)).unwrap();
    assert!(matches!(
        notice(&mut rig.0).await,
        Notice::Event(Event::InputStarted { lineage, .. }) if lineage.request == request(2)
    ));
    rig.0
        .try_audio(request(2), 0, std::time::Instant::now(), &[29; 160])
        .unwrap();
    assert!(
        dial.service.receive().await["realtimeInput"]
            .get("audio")
            .is_some()
    );
    rig.0.try_end(request(2)).unwrap();
    assert!(
        dial.service.receive().await["realtimeInput"]
            .get("activityEnd")
            .is_some()
    );
    dial.service.send(audio(29, SAMPLES)).await;
    dial.service.send(idle()).await;
    assert!(
        matches!(notice(&mut rig.0).await, Notice::Event(Event::Audio { lineage, .. }) if lineage.request == request(2))
    );
    assert!(
        matches!(notice(&mut rig.0).await, Notice::Event(Event::TurnComplete { lineage, idle: true }) if lineage.request == request(2))
    );
    assert!(rig.0.is_connected());
}

#[tokio::test(start_paused = true)]
async fn successor_failure_replaces_only_the_already_retired_drain() {
    let (mut rig, mut replacement) = with_unread_answer(true).await;
    rig.0.try_start(request(2)).unwrap();
    rig.0
        .try_audio(request(2), 0, std::time::Instant::now(), &[31; 160])
        .unwrap();
    rig.0.try_end(request(2)).unwrap();
    for field in ["activityStart", "audio", "activityEnd"] {
        assert!(
            replacement.service.receive().await["realtimeInput"]
                .get(field)
                .is_some()
        );
    }
    replacement.service.send(audio(31, SAMPLES)).await;
    replacement
        .service
        .send(json!({"serverContent":{"generationComplete":true}}))
        .await;
    replacement
        .service
        .close(1011, "You exceeded your current quota")
        .await;
    observe_link_failure(&mut rig).await;
    let mut received = 0;
    let mut completed = 0;
    loop {
        match raw(&mut rig.0).await {
            Ok(Notice::Event(Event::Audio { lineage, pcm, .. })) => {
                assert_eq!(lineage.session.get(), 2);
                assert_eq!(lineage.request, request(2));
                assert!(pcm.iter().all(|sample| *sample == 31));
                received += pcm.len();
            }
            Ok(Notice::Settled { lineage, .. }) => {
                assert_eq!(lineage.request, request(2));
                assert_eq!(received, SAMPLES, "terminal outcome preceded PCM");
                completed += 1;
            }
            Ok(Notice::Event(_) | Notice::Unavailable { .. }) => {}
            Ok(other) => panic!("unexpected successor notice {other:?}"),
            Err(failure) => {
                assert_eq!(failure.error, Error::QuotaExceeded);
                break;
            }
        }
    }
    assert_eq!(received, SAMPLES);
    assert_eq!(completed, 1);
}

#[tokio::test(start_paused = true)]
async fn shutdown_drops_both_unread_answer_and_pending_replacement_immediately() {
    let (mut rig, mut replacement) = with_unread_answer(true).await;
    replacement.service.close(1011, "replacement loss").await;
    observe_link_failure(&mut rig).await;
    let _unaccepted_third = next_dial(&mut rig.1).await;
    let before = Instant::now();
    rig.0.shutdown();
    assert!(rig.0.draining.is_none());
    assert!(!rig.0.is_connected());
    // Outage receipts may already be pending. No retained PCM or new setup may
    // appear after the synchronous stop, and no retry timer has to expire.
    for _ in 0..4 {
        match raw(&mut rig.0).await {
            Ok(Notice::Unavailable { .. }) => {}
            Ok(other) => panic!("post-shutdown event {other:?}"),
            Err(failure) => {
                assert_eq!(failure.reason, FailureReason::Shutdown);
                assert_eq!(before.elapsed(), Duration::ZERO);
                return;
            }
        }
    }
    panic!("shutdown did not end within its bounded pending notices");
}

#[tokio::test(start_paused = true)]
async fn refused_start_during_recovery_does_not_supersede_the_buffered_answer() {
    let (mut rig, mut replacement) = with_unread_answer(true).await;
    replacement
        .service
        .close(1011, "You exceeded your current quota")
        .await;
    observe_link_failure(&mut rig).await;
    assert_eq!(rig.0.try_start(request(2)), Err(Error::NotConnected));
    let mut output = Vec::new();
    let mut terminal = Vec::new();
    while let Ok(notice) = raw(&mut rig.0).await {
        audio_samples(notice, &mut output, &mut terminal);
    }
    assert_eq!(output, expected_pcm());
    assert_eq!(terminal, ["settled"]);
}
