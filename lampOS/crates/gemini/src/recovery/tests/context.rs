//! Recovery reports advertised context provenance, not model-memory coverage.
use super::*;

fn invalidation() -> Value {
    json!({"sessionResumptionUpdate":{"newHandle":"","resumable":false}})
}

async fn next_context(
    rig: &mut Rig,
    previous: &mut Dial,
    expected: Context,
    retained_handle: &str,
) -> Dial {
    previous.service.close(1011, "scripted link loss").await;
    let (notice, mut dial, setup) = ready(rig).await;
    let Notice::Ready { context, .. } = notice else {
        panic!("replacement readiness: {notice:?}");
    };
    assert_eq!(context, expected);
    assert!(dial.resumed);
    assert_eq!(
        setup["setup"]["sessionResumption"]["handle"],
        retained_handle
    );
    assert!(
        dial.service
            .try_receive(Duration::from_millis(20))
            .await
            .is_none(),
        "recovery must not replay input to repair missing context"
    );
    dial
}

async fn resumed_connection() -> (Rig, Dial) {
    let (mut rig, mut a) = connected(
        RecoveryPolicy {
            max_recoveries: 8,
            ..quick()
        },
        config(),
    )
    .await;
    let b = next_context(&mut rig, &mut a, Context::Resumed, "HANDLE-ONE").await;
    assert_eq!(b.session.get(), 2);
    (rig, b)
}

async fn complete_exchange(rig: &mut Rig, dial: &mut Dial, id: u64) {
    speak(rig, dial, id, true).await;
    dial.service.send(audio(17, 240)).await;
    dial.service.send(idle()).await;
    assert!(matches!(
        notice(&mut rig.0).await,
        Notice::Event(Event::Audio { lineage, .. }) if lineage.request == request(id)
    ));
    assert!(matches!(
        notice(&mut rig.0).await,
        Notice::Event(Event::TurnComplete { lineage, idle: true }) if lineage.request == request(id)
    ));
}

#[tokio::test(start_paused = true)]
async fn inherited_point_is_not_reported_current_after_a_later_session_exchange() {
    let (mut rig, mut b) = resumed_connection().await;
    complete_exchange(&mut rig, &mut b, 1).await;
    let _c = next_context(&mut rig, &mut b, Context::ResumedBeforeLatest, "HANDLE-ONE").await;
}

#[tokio::test(start_paused = true)]
async fn an_idle_replacement_without_metadata_keeps_the_advertised_current_point() {
    let (mut rig, mut b) = resumed_connection().await;
    let _c = next_context(&mut rig, &mut b, Context::Resumed, "HANDLE-ONE").await;
}

#[tokio::test(start_paused = true)]
async fn an_actual_new_current_handle_replaces_the_inherited_point() {
    let (mut rig, mut b) = resumed_connection().await;
    complete_exchange(&mut rig, &mut b, 1).await;
    b.service.send(handle("HANDLE-TWO")).await;
    let _c = next_context(&mut rig, &mut b, Context::Resumed, "HANDLE-TWO").await;
}

#[tokio::test(start_paused = true)]
async fn a_new_handle_followed_by_invalidation_is_preserved_but_reported_partial() {
    let (mut rig, mut b) = resumed_connection().await;
    complete_exchange(&mut rig, &mut b, 1).await;
    b.service.send(handle("HANDLE-TWO")).await;
    b.service.send(invalidation()).await;
    let _c = next_context(&mut rig, &mut b, Context::ResumedBeforeLatest, "HANDLE-TWO").await;
}

#[tokio::test(start_paused = true)]
async fn invalidation_without_a_local_handle_applies_to_the_inherited_point() {
    let (mut rig, mut b) = resumed_connection().await;
    // No input and no B-issued handle: the invalidation itself is still evidence.
    b.service.send(invalidation()).await;
    let mut c = next_context(&mut rig, &mut b, Context::ResumedBeforeLatest, "HANDLE-ONE").await;
    let _d = next_context(&mut rig, &mut c, Context::ResumedBeforeLatest, "HANDLE-ONE").await;
}

#[tokio::test(start_paused = true)]
async fn later_idle_outages_cannot_restore_an_inherited_points_freshness() {
    let (mut rig, mut b) = resumed_connection().await;
    complete_exchange(&mut rig, &mut b, 1).await;
    let mut c = next_context(&mut rig, &mut b, Context::ResumedBeforeLatest, "HANDLE-ONE").await;
    let mut d = next_context(&mut rig, &mut c, Context::ResumedBeforeLatest, "HANDLE-ONE").await;
    let _e = next_context(&mut rig, &mut d, Context::ResumedBeforeLatest, "HANDLE-ONE").await;
}

#[tokio::test(start_paused = true)]
async fn fresh_advertisement_after_partial_recovery_reports_its_own_metadata() {
    let (mut rig, mut b) = resumed_connection().await;
    b.service.send(invalidation()).await;
    let mut c = next_context(&mut rig, &mut b, Context::ResumedBeforeLatest, "HANDLE-ONE").await;
    complete_exchange(&mut rig, &mut c, 1).await;
    c.service.send(handle("HANDLE-THREE")).await;
    let _d = next_context(&mut rig, &mut c, Context::Resumed, "HANDLE-THREE").await;
}
