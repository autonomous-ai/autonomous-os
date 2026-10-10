# Provider availability and request outcomes

This is a prerequisite for coordinator outage handling. The runtime still uses
the legacy provider contract; typed recovery remains disabled. The changes here
do not yet make the listening cue, coordinator admission or failure speech
outage-aware. No device, cloud or acoustic qualification follows from them.

## Separate connection progress from answer consumption

The Gemini supervisor has an additive split interface:

- `poll_link()` performs bounded nonblocking upkeep and returns one
  `LinkUpdate`, without consuming answer PCM or spending output packet credit.
- `next_output()` returns ordered answer events, `Settled` or `TurnLost`.
  A terminal link update does not discard buffered output or its final outcome.
- Existing `next()` remains the combined compatibility interface. Do not mix
  it with the split interface on one supervisor.

Split consumers must poll the link regularly, including when their answer
buffer is full. Cancel the pending `next_output()` future on the control tick,
poll the link, and then resume output consumption. A pending link notification
holds the data-facing future without busy polling. The supervisor stores one
pending link update, one ordered terminal outcome, one live connection or
attempt, and at most one failed connection's output drain.

Each link update carries a checked, increasing revision and source session.
Original local setup-completion and failure-observation times survive delayed
consumption; taking a notification does not restart its outage budget. A Ready
that became false before consumption must not reopen submission. A Close still
unread behind the deliberately bounded WebSocket PCM buffer has not yet been
observed. These timestamps do not claim to measure when the remote network
actually failed.

## Report the request that actually failed

In the opt-in typed worker path, `not_delivered` now means Start was refused
while no connection existed. A refusal of Audio or End after a successful Start
leaves the accurate terminal stage to the supervisor. Earlier received PCM
still precedes that terminal outcome, and no input is replayed.

One refused Start can await report space. The worker pauses input consumption
until that report fits the existing 512-item output bound. Stop, privacy and
local retirement continue to run. A later refusal cannot overwrite the pending
outcome, and a newer request number cannot suppress an older ordered failure.

After reconnect, leftover input from the old session is discarded only with
proof that its exact successfully submitted request belongs to a replaced
connection. This covers an old Audio/End and the implicit End before a successor
Start. An unrelated `StaleRequest` remains an error. Legacy continues to fail
closed; the typed behavior does not silently enable production recovery.

## Coordinator integration still required

Keep cloud submission availability in the sole coordinator. The existing
interaction Snapshot describes hard local privacy, capture and admission
leases. Closing those leases revokes an active owner; a network outage alone
must not use that path to truncate already received speech.

The coordinator must consume bounded priority link events before new input,
with boot, session, revision and original observation time. Older credited
Ready packets must not override a newer outage. Link events must bypass audio
credits; per-request terminal outcomes stay ordered after their PCM. Startup,
normal operation, fixtures and evaluation must use the same contract.

Preserve an already submitted reply while its local authority remains valid.
Require its matching `Started` receipt before speech dispatch; that receipt is
local session submission evidence, not server or acoustic acknowledgement.
During an outage, an accepted local interruption may cancel the current owner
without allocating a replacement or sending Start. Drain the unsubmitted
utterance through its endpoint so recovery cannot reinterpret its remaining
words as a new request. Privacy, stop and hard lease expiry still revoke output.

The target remains at most 20 ms from locally observed outage to publication of
closed coordinator submission/readiness, including full PCM backpressure. This
end-to-end local boundary is not yet implemented or measured by this checkpoint.

## Audible failure delivery

Reuse V1's cache-only failure notice and cancellation checks, not its generic
cache-miss synthesis or ownerless callback. The Rust fixture loader already
validates bounded regular-file WAVs, SHA256 and mono 24 kHz PCM16. Reuse its
loading and existing guarded speaker path for approved preloaded failure audio;
do not reuse the one-reply experiment state machine as a product service.

No failure voice asset is packaged in this source. Provisioning a versioned
asset with transcript, voice, rate, duration and hash remains required. A missing
asset must report unavailable notice capability; an outage must not cause another
cloud synthesis request. Failure playback needs its original still-current owner,
separate playback occurrence and retirement/cancellation evidence. It must not
be counted as a successful answer or improve answer latency metrics.

Source audit: V1 main `d5efe9d7b73cc529b34cd4abe97624682a82ca94`,
`hal/drivers/voice/tts/service.py` cache-only quota handling and generic cache
misses, `hal/drivers/voice/_internal/device_retry_feedback.py` validity checks,
and realtime orchestrator retry/stop handling. These are design references, not
runtime or source dependencies of standalone lampOS.

## Verification boundaries

Deterministic regressions reproduce two worker failures before their fixes:
an accepted request incorrectly reported as not delivered, and old-session input
terminating a healthy replacement with `StaleRequest`. Tests cover output-space
exhaustion, pending-report retention, stop precedence, exact replaced-session
proof and unchanged Legacy failure behavior. Supervisor tests separately cover
progress without PCM consumption, notification bounds, original-time budgets,
setup-then-close, ordered drain outcomes and local retirement.

Compatibility tests and simulated time do not measure physical conversation,
microphone echo suppression, speaker latency, addressee detection or naturalness.
The earlier full ARM qualification belongs to source `a19382a9` in
[provider recovery integration](provider-recovery-integration.md). The new
source has the separate, narrower ARM qualification below.

## Source qualification, 2026-10-11

All final gates used the same frozen standalone source export above
`ce6d476bb9b1b0d831afbef2e71a4d8fbbf70a5b`. The 239-file source manifest SHA256 is
`e52f520c0b04b02331db05c645bd7a9017c9e6f7a36a7f17fec323cad4e4662c`.
The 174-file Rust/Cargo/toolchain manifest SHA256 is
`bc9e6ba2b1905269575e031afab6af5e96033ccb53800f4fbecd7b342f51a3d3`.
Every source file was checked unchanged before and after the gates. Later
qualification prose changes only this note and `HANDOFF.md`; tested runtime
and fixtures remain byte-for-byte identical.

Host: x86_64 macOS, Rust 1.96.0, a fresh target directory and two build jobs.
These commands passed sequentially:

```sh
cargo fmt --all -- --check
cargo clippy --locked --offline --workspace --all-targets -- -D warnings
cargo test --locked --offline --workspace --no-fail-fast -- --test-threads=1
cargo build --locked --offline --release -p lamp-live -p lamp-voice-eval
```

The full host workspace passed **843 tests, zero failures, three ignored**.
The Gemini package contributes 130 passing tests; provider-worker tests
contribute 44. The ignored 1,600-turn soak and 60-/180-second paced tests were
not rerun for this source. Earlier results for those tests do not qualify this
checkpoint.

Linux/ARM64: Rust 1.96.0, GCC 12.2, ARM ALSA 1.2.8, QEMU 7.2.22, two build jobs
and one test thread. The local container had no network, read-only source and
registry/git caches, and a new target volume. Its image digest is
`sha256:e31a0129766cdbe6714dc6c1acc10d8ab318d1333cc1901c001590ff6833834e`.
The runner was `qemu-aarch64-static`, with `QEMU_LD_PREFIX` explicitly unset.
Formatting passed again, followed by these commands:

```sh
cargo clippy --workspace --all-targets --target aarch64-unknown-linux-gnu --locked --offline -- -D warnings
cargo build --release --target aarch64-unknown-linux-gnu --locked --offline -p lamp-live -p lamp-voice-eval
cargo test -p lamp-gemini --release --target aarch64-unknown-linux-gnu --locked --offline -- --test-threads=1
cargo test -p lamp-live --lib --release --target aarch64-unknown-linux-gnu --locked --offline provider_worker::tests -- --test-threads=1
```

The targeted ARM tests passed **174 tests, zero failures, three ignored**.
Both release executables and the executed tests were verified as AArch64 ELF.
The full ARM workspace and ignored long tests were not rerun. The local VM and
keeper were stopped after verification; source, evidence and caches remain.
Emulated test durations are not native-board performance measurements.

The preserved first Gemini run failed four tests: one local TLS fixture lacked
the required local-socket permission, and three playback fixtures deliberately
delayed polling for as long as their two-second test outage budget. Those three
fixtures now use the production 15-second outage budget, retaining the other
quick test settings. Explicit expired-budget and late-setup regressions remain
strict; no production timeout was widened. Final permitted local-fixture runs
pass. Earlier failures, source identities and before/after worker regressions
remain in the evidence rather than being replaced by final green logs.

Evidence is retained in ignored `artifacts/provider-availability-20261011/`
and a separate durable handoff backup. The bundle includes frozen source,
manifests, exact commands, raw host/ARM logs, source verification, reproduced
failures and resource shutdown receipts. It contains no deployment. There were
no cloud calls or physical microphone, speaker, camera or actuator tests.
