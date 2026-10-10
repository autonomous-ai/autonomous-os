# Provider recovery integration

This combines shared checkpoint `000d5a77f` with Agent 1's committed
`d6b813695f2bda2f5ea02a43671540251ea79a36`, preserving the newer playback
occurrence and ring evidence fixes. No physical device or cloud service is
accessed by this qualification.

## Reviewed behavior

The transport holds bounded output while the worker delivers at playback
speed. Its no-consumption watchdog is 30 seconds, replacing the earlier
two-second bound that conflicted with ordinary backpressure. The coordinator's
30-second PCM limit remains a backlog limit. The worker reserves every output
packet under the existing two-packet capacity contract and drops queued output
for retired requests. Neither IPC freshness nor the input/Stop service path is
relaxed.

Connection replacement can start while received audio from the old connection
drains. No input or answer is replayed. A generation that completed before the
disconnect can settle after delivery; an incomplete request gets an explicit
lost-request outcome. Production still uses legacy reporting: a lost request
ends the worker and the coordinator records a failed run. Spoken failure and
surviving that failed turn are not implemented by this integration.

Review found additional failures, which receive focused regressions:

- A second disconnect could overwrite the first connection's single drain
  slot, losing already received PCM and its terminal outcome.
- A reconnect while later children were starting emitted a second `Ready`
  that startup rejected. Startup now consumes repeated readiness using the
  same cumulative packet counters; it still rejects unsolicited answer output.
- Agent 1 enabled an assumed cancellation barrier in the worker. That option
  can attribute a delayed old answer to a new request without independent wire
  evidence. Shared integration keeps `Require`; the explicit assumption mode
  remains tested but is not the production default.
- Recovery could retain connection A's current-context flag after connection B
  accepted newer input without advertising a new resumption handle. Retained
  points now carry their source session, and explicit invalidation survives
  even without a replacement handle. The runtime conservatively reports known
  older context; this is not proof of exact restored model memory. See
  [resumption provenance](resumption-provenance.md).
- The evaluator's partial-trace fixture could exit before the runner's first
  ping, producing a startup BrokenPipe instead of the intended mid-trace
  failure. It now reads that ping before emitting its partial trace and closing.
  The strict `partial` reason and invalid-attempt assertions remain. No relay
  or production evaluator behavior changes.
- A diagnostic writer could observe an empty queue, then observe producer
  shutdown after new records had been accepted, and exit without draining
  those records. It now checks the queue again after observing shutdown or a
  producer fault. Five deterministic regressions retain the accepted 160
  capture frames and preserve the distinction between completed and invalid
  recordings. Queue bounds and shutdown deadlines are unchanged. This repairs
  recording evidence, not speaker output or acoustic latency. See
  [diagnostic shutdown](diagnostic-shutdown.md).

Keeping `Require` has a real limitation: if the service never confirms a
cancelled response, the two-second barrier ends the session. The five live
service probes in the [provider handoff](gemini-session-reliability.md#service-probes)
remain necessary. A timeout cannot be described as natural pause/resume, and
an assumed owner cannot be counted as a qualified answer.

## Initial integration evidence

The retained before/after logs distinguish regression reproduction from final
combined gates. All gates below use a source-only export outside the parent
repository. Its 172-file Rust/Cargo/toolchain manifest SHA256 is
`062614884fae10036f248dca3b2d80e1a56e5e77f777f284be8e25aa6010afdd`.
The complete source was checked unchanged after each gate. Later documentation
records those results; it does not change the tested runtime or fixtures.
Agent 1's earlier ARM64 emulation and host tables apply only to their recorded
commits; no earlier gate qualifies this newer source.

Host: x86_64 macOS, pinned Rust 1.96.0, fresh target directory, four build jobs.
These exact commands passed, sequentially:

```sh
cargo fmt --all -- --check
cargo clippy --locked --offline --workspace --all-targets -- -D warnings
cargo test --locked --offline --workspace --no-fail-fast -- --test-threads=1
cargo build --locked --offline --release -p lamp-live -p lamp-voice-eval
cargo test --locked --offline -p lamp-gemini --lib soak -- --ignored --nocapture
cargo test --locked --offline -p lamp-live --lib follows_output_capacity -- --ignored --nocapture --test-threads=1
```

The ordinary workspace run passed **810 tests, zero failed, three ignored**.
The next two commands explicitly ran all three ignored tests: one 1,600-turn
scripted fault soak and two long-answer delivery tests. All passed. The soak
injects cancellations, disconnects and lost turns and also exercises the
explicit assumed-barrier policy; it is not 1,600 successful human conversations.
Production's `Require` policy has its own passing cancellation-barrier regression.

The long-answer tests connect a scripted service through provider output
capacity to a local IPC reader and a modeled speaker. They validate complete,
ordered delivery under backpressure, not the real coordinator, speaker or room.

| Answer audio | Deliberate hold | Playback finished | Modeled starvation | Delivered packets | Peak PCM backlog |
|---|---:|---:|---:|---:|---:|
| 60 s | 10.000 s | 70.033 s | 0 ms | 1,500 | 30.00 s |
| 180 s | 20.001 s | 200.053 s | 3 ms | 4,500 | 30.00 s |

Linux/ARM64 strict workspace/all-target Clippy and both release binaries also
passed using Rust 1.96.0 and the same immutable source in an offline container.
The binaries are ARM aarch64 ELF files. The corrected QEMU test run finished
with **848 passed, one failed, three ignored**. Its sole failure was
`audio_fault_end_is_incomplete_even_with_good_written_pcm`: capture frames were
zero rather than 160, while the report correctly remained invalid and omitted
the completion marker. This failure is retained, not counted as a passing run.

Before that run, an incompatible `QEMU_LD_PREFIX` caused the first test process
to spin without completing. A controlled comparison of the same binary timed
out at 15 seconds with the override and passed in 0.03 seconds without it.
The override selected cross libc `2.36-8cross1` instead of the installed arm64
libc `2.36-9+deb12u14`. Removing it corrected the runner without a source or
deadline change. The intentionally stopped first run, loader hashes and both
commands remain in the evidence. Neither cross-compilation nor emulated
execution measures Orange Pi timing, ALSA hardware behavior, room audio or
physical choreography.

Focused red/green evidence includes drain/input-lineage failures, the strict
production barrier, repeated startup readiness and the deterministic
partial-trace fixture. The drain review's ten
new cases pass. In the reproduced repeated-disconnect failure, the old source
retained zero of 960 accepted samples; the correction retains the answer and
its terminal outcome until delivery or explicit retirement. These are software
ownership/delivery boundaries, not acoustic response-time improvements.

The failed ARM diagnostic case prompted the shutdown investigation above. The
original run did not record its exact thread interleaving. The independently
reproduced race explains how accepted records can disappear; its regression
tests force that interleaving rather than depending on scheduler timing.

## Final combined qualification

The resumption provenance and diagnostic shutdown corrections require a new
source identity. The frozen combined source contains 173 Rust/Cargo/toolchain
files; its manifest SHA256 is
`a19382a9fba8c21103104ff2f6f853a11dfa489eb64d4a684300ba4ad5f735aa`.
The six host commands listed above passed again on this immutable source:
**822 ordinary tests, zero failed, three ignored**, followed by explicit
passing runs of all three ignored tests. Formatting, strict workspace/all-target
Clippy and both release binaries also pass. The complete source was checked
unchanged after each command. Documentation was then updated separately.

| Answer audio | Deliberate hold | Playback finished | Modeled starvation | Delivered packets | Peak PCM backlog |
|---|---:|---:|---:|---:|---:|
| 60 s | 10.001 s | 70.028 s | 1 ms | 1,500 | 30.00 s |
| 180 s | 20.000 s | 200.046 s | 1 ms | 4,500 | 30.00 s |

These are the same scripted service, local IPC reader and modeled speaker
boundaries as the initial table. The durations include deliberate playback
holds; they do not measure speech-end to first audible word. The 1,600-turn
soak passes request accounting under injected faults; intentionally cancelled
and lost requests remain in its tally.

Linux/ARM64 formatting, strict Clippy, both release builds and full release
test compilation pass. The final release workspace suite passed **861 tests,
zero failed, three ignored**, including all 18 diagnostics integration tests
and all 11 synthetic physical-loopback evaluator tests. All three ignored tests
then passed in explicit ARM runs: the 1,600-turn injected-failure soak and both
long-answer tests. The earlier **848/1/3** result remains recorded above; it was
not erased or reclassified.

| ARM modeled answer | Deliberate hold | Playback finished | Modeled starvation | Delivered packets | Peak PCM backlog |
|---|---:|---:|---:|---:|---:|
| 60 s | 10.002 s | 70.025 s | 0 ms | 1,500 | 30.00 s |
| 180 s | 20.000 s | 200.041 s | 0 ms | 4,500 | 30.00 s |

These exercise the same modeled path as the host table. The test summary's
static suffix says `host`; the commands and inspected ELF identify these runs
as ARM64 under QEMU. The raw logs remain unchanged.

The ARM builder uses Rust 1.96.0, Debian bookworm, aarch64 GCC 12.2 and QEMU
7.2.22. Source and dependency caches are mounted read-only, the network is
disabled, and each gate runs against the same pinned container image. Its image
ID is `44442b1b138ace391dbeab57edfb2673bec55cb39f2a287e92a3dfa100f452fc`.
`QEMU_LD_PREFIX` is explicitly unset. The Cargo commands inside that container
are:

```sh
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --target aarch64-unknown-linux-gnu --locked --offline -- -D warnings
cargo build --release --target aarch64-unknown-linux-gnu --locked --offline -p lamp-live -p lamp-voice-eval
cargo test --workspace --release --no-run --target aarch64-unknown-linux-gnu --locked --offline
cargo test --workspace --no-fail-fast --release --target aarch64-unknown-linux-gnu --locked --offline -- --test-threads=1
cargo test -p lamp-gemini --release --target aarch64-unknown-linux-gnu --locked --offline prolonged_conversations_with_injected_failures_keep_every_request_accounted_for -- --ignored --nocapture --test-threads=1
cargo test -p lamp-live --release --target aarch64-unknown-linux-gnu --locked --offline sixty_second_answer_follows_output_capacity -- --ignored --nocapture --test-threads=1
cargo test -p lamp-live --release --target aarch64-unknown-linux-gnu --locked --offline three_minute_answer_follows_output_capacity -- --ignored --nocapture --test-threads=1
```

Both release-build binaries were inspected as ARM aarch64 ELF executables:

- `lamp-live` SHA256:
  `30e2c34b105fb561ed547cc9c7f85663250daeea81b2a45d4072cc76287f6d5a`.
- `lamp-voice-eval` SHA256:
  `32adfe2f13e2be3317fcec1b9513b2b8434919a39b7f04d5e40a9d36a10f1460`.

Later package-specific test compilation replaced `release/lamp-live`; its final
hash is `d5793e8ad054bdd710d38f4e36d6324dd77193b015d6504bfbf0a55810c38434`.
The receipt preserves both phases and their commands. The evaluator hash is
unchanged. No binaries were copied or deployed.

The different host and ARM totals include Linux-only tests. The emulator does
not qualify the installed drivers, microphones, speaker, camera, motors or
ring. No V1-versus-V2 acoustic speedup or natural conversation claim follows
from these gates.

Retained evidence is in `artifacts/provider-recovery-20261011/`: immutable and
documentation-updated source archives, per-file hashes, commands, raw logs,
exit statuses, focused red/green cases and both initial and final ARM results.
The evidence manifest also records which Markdown files were updated after
the gates. Runtime, fixture and configuration bytes match the frozen source.

## Next contract work

The next [provider availability prerequisite](provider-availability.md) adds a
split supervisor control/output API and corrects opt-in request failure tracking.
Its coordinator, priority IPC, readiness and audible-failure integration is still
pending. The final qualification above identifies the earlier source only.

The typed statuses in the provider worker remain disabled. Simply enabling
them is not sufficient:

- Admission and readiness must close promptly during an outage, independently
  of a full audio buffer. Currently status observation and transmission share
  playback backpressure. A separate control status path also needs supervisor
  connection progress while PCM consumption is paused; merely changing sockets
  cannot remove the observation delay.
- Closing new-input availability must preserve already-owned output when
  capture, privacy and output permits remain valid. The existing hard admission
  gate revokes the active turn and speech requires listening readiness. It
  cannot be reused as a network-availability switch without truncating the
  answer this integration preserves. Physical privacy still takes precedence.
- Local interruption must remain possible while cloud submission is unavailable.
  Closing the submission gate must not prevent the listener from stopping
  buffered speech. A new question that was not submitted needs its own accurate
  outcome; it must not be silently queued for a later connection or confused
  with an accepted request whose answer was lost.
- Link availability can use bounded priority control events. A turn's terminal
  failure should remain ordered after its received PCM; placing it on another
  socket instead requires an explicit data-delivery fence. Packet credit
  counters must remain cumulative, including discarded old-owner data.
- The opt-in worker now distinguishes refused Start from refused Audio/End
  after accepted Start, preserving the supervisor's terminal stage. Coordinator
  consumption and per-owner evaluation of these outcomes remain to integrate.
- Tick-generated refusal reports now respect the output bound and pause intake
  before overwriting a pending report. The urgent status queue still needs a
  bounded priority control implementation; allocated capacity is not a bound.
- Startup, normal operation and the evaluator must consume the same status
  contract. Any assumed ownership must taint the correct answer, including an
  assumption that precedes identification of its successor.
- Resumption context remains limited to source-session provenance and known
  invalidation. No context status proves that the latest exchange or every
  client message survived. Legacy exposes it through a diagnostic line, not a
  memory guarantee.
- A locally retired open request from a failed connection is rejected if its
  remaining End targets the replacement. Legacy fails closed; opt-in typed
  recovery now discards it only with exact replaced-connection lineage proof.
- Failure speech must use valid current output ownership, never old callbacks
  or a fabricated successful completion. Privacy and stop remain authoritative.

The proposed local target is at most 20 ms from an observed provider outage to
publication of closed submission/readiness state, including under full PCM
backpressure. This excludes the network's failure-detection delay and is not
yet measured. Tests must separately cover retained playback, a local
interruption during that outage, privacy closure, stale recovery events and a
new input arriving on the same loop iteration as the outage.

Until those changes and their tests land, repeated `provider_ready` plus fixed
category diagnostic lines are the legacy recovery interface. This integration
does not claim social addressee detection, echo-safe interruption, physical
latency improvement, or owner acceptance.
