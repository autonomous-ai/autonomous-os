# Complete the Rust implementation: next 24-hour work allocation

Owner instruction: implement everything possible without the physical Lamp,
integrate the three workstreams, then test on the real device. This is an
implementation plan, not a claim that the rewrite or natural interaction is
finished. The target is a complete Rust candidate; the actual outcome after
24 hours must list any unfinished code explicitly.

Starting shared checkpoint: `3a21db25d89532a5aadb7db40bf46505d15cccba` on
`lamp-v2-chat`. Agent 1's `d6b813695` and Agent 2's `3eaa0365` are already ancestors
of that checkpoint. Preserve existing branch history and uncommitted work when
incorporating a newer shared checkpoint. Do not start again from their old
pre-integration source.

## Scope and observed gaps

The release is chat with a physical character on the left or right of one
person's desk. It includes hearing, vision, installed sensors and controls,
purposeful speech, head/body movement, light and silence. Harness, agentic tasks,
new long-term memory, fleet services and the legacy web UI remain outside it.
Keep all required runtime, configuration, assets/provisioning and documentation
inside standalone `lampOS`, with Rust runtime code and English notes.

The source currently composes capture, speaker, privacy and provider workers,
plus an optional ring worker. It does not compose the whole robot. In particular:

- The coordinator is a directed qualification session capped at 600 seconds;
  privacy closure and child exit terminate it.
- Admission still immediately accepts directed-session VAD. Echo, backchannels,
  other-person speech and hesitant utterances need implemented evidence and
  decision paths, not merely system-prompt instructions.
- Provider recovery foundations exist, but coordinated availability and audible
  failure delivery are not enabled in the shared runtime.
- Motor code has read-only protocol/calibration inspection and no motion API.
- The camera inspector passes metadata; live frame delivery, perception and
  conversational visual context are missing.
- Environmental state has typed snapshots but no acquisition drivers. Ordinary
  touch/button handling is missing; the current GPIO worker handles privacy.
- The evaluator's fake policy and fake executable are not the production
  coordinator. They cannot prove that the actual process graph works.

## Main agent: interaction owner, service and final integration

Own `crates/interaction/**`, `crates/motor/**`, `crates/ring/**`,
`crates/live/src/coordinator/**`, `coordinator.rs`,
`admission.rs`, `activity.rs`, `choreography.rs`, `process.rs`, shared wire/config/
CLI registration, physical-control workers, runtime packaging and the shared
integration branch.

1. Finish priority provider availability, request outcomes, readiness and
   cancellation integration. Preserve received speech through outages, retain
   exact request identity, drain unsubmitted utterances through their endpoint,
   and never replay them after recovery. Keep hard privacy independent of cloud
   availability. Integrate cached failure notices without counting them as
   successful answers.
2. Implement admission and turn policy using explicit acoustic and visual
   evidence from the other workstreams. Cover addressed interruption, opening
   words, acknowledgments/asides, calls, colleagues, other-device speech,
   silence, uncertainty, short answers and mid-question pauses. Missing or stale
   evidence must remain missing; looking at the monitor is not a rejection rule.
3. Implement the five-joint motor worker with one serialized bus owner,
   calibrated targets, bounded trajectories, readback, range/velocity/acceleration
   checks, overload/fault lockout, cancellation and shutdown handling. Reuse
   audited V1/SDK protocol semantics. Inherited normalized values are not
   degrees or proven safe limits. Default motion remains inhibited until the
   future physical placement, calibration and units checks pass.
   Extend the choreographer to coordinate actual listening, waiting, playback,
   cancellation and settling across ring, head and body. Bound repetition and
   gesture duration. Preserve stillness; prohibit stock spoken fillers, room
   scans and obligatory gestures. All intents keep original ownership through
   final actuator validation.
4. Add an ordinary long-running service entry point. Retain the finite directed
   CLI for qualification. Implement mute/unmute, signal shutdown, bounded
   worker recovery, new worker incarnations, dependency readiness and bounded
   diagnostics/trace storage. A failed or restarted worker must not revive
   earlier output authority or silently claim readiness.
5. Implement ordinary touch/button events and compose camera, perception,
   motor, controls and configured sensor workers.
   Add standalone configuration, per-device calibration provisioning and a desk
   setup flow for left/right placement, sight, hearing and audibility. Saved
   data alone must not authorize movement or prove current placement.
6. Integrate both branches incrementally, fix interface conflicts, run combined
   host and Linux/ARM64 checks and source-only export checks, and prepare the
   exclusive device cutover/rollback procedure. Preserve V1 for comparison and
   rollback until the replacement qualifies.

Temporary shared-file lock: the main agent currently owns `provider_worker.rs`
and the new provider-link wire integration. Agent 1 should review it and submit
proposed changes separately until the main agent publishes that contract. This
does not transfer ownership of `crates/gemini/**` away from Agent 1.

## Agent 1: complete the voice stack and sensor context

Own `crates/gemini/**`, `crates/audio/**`, `crates/environment/**`, a new
environmental worker, and the audio-specific live files
`audio_workers.rs`, `playback.rs` and `reference.rs`, plus focused tests/docs.
Coordinate shared wire/API changes with the main agent before editing them.

1. Review the integrated `poll_link` / `next_output` implementation. Add
   adversarial regressions for slow consumers, repeated disconnects, retained
   drains, delayed readiness, context provenance, exact terminal outcomes and
   bounded recovery. Keep cancellation policy `Require`; an assumption is not
   provider confirmation. Prepare the narrow live-service probes without
   claiming they ran.
2. Implement and verify the continuous audio path: accepted-speaker PCM as echo
   reference, capture/render clock behavior, discontinuities, privacy changes,
   device stalls and restart. Add bounded echo/near-end/double-talk evidence for
   the main agent's admission decision. VAD alone cannot identify the addressee
   or prove that a sound is human speech.
3. Test actual provider-worker output at speaking pace and with consumption
   stopped across its bounds. Measure the failure threshold rather than repeat
   the derived approximately 28-second estimate. Cover long answers, bursts,
   pauses/resumption and successive interruptions; preserve all opening input.
4. Implement the Gemini input for bounded fresh visual context supplied by
   Agent 2, with original privacy/frame lineage and cancellation. It must not
   block audio input, local cancellation or output playback.
5. Provide the main agent a bounded cache-only failure-audio loader/provisioning
   contract using the existing validated PCM loader where suitable. Include
   transcript, approved voice, format, duration, hash and missing-asset behavior.
   Test fixtures are not approved production voice assets; no synthesis request
   may be required to speak about a provider outage.
6. Implement acquisition for explicitly configured sensor profiles from the
   audited hardware contract. Preserve warmup, CRC/status errors, typed units,
   per-field freshness and bounded retry. Serialize shared-bus transactions
   and isolate acquisition from audio. Default disabled profiles stay disabled;
   supported profiles do not establish what is installed. Complete the voice
   path before optional environmental refinements.

Deliver actual code and before/after regression evidence, CPU/RSS and processing
budget measurements with exact boundaries. Separate deterministic replay,
host timing, emulated ARM, cloud protocol and physical acoustic evidence.

## Agent 2: camera, perception and acceptance infrastructure

Own `crates/camera/**`, `crates/voice-eval/**`, `crates/acoustic/**`,
`crates/observer/**`, camera-specific live files and new perception/frame-delivery
modules. Keep the main agent as the writer of shared coordinator, wire,
CLI and workspace registration; agree the interfaces before implementation.

1. Complete bounded camera-frame delivery and a separate Rust-controlled
   perception worker. Supply original frame identity, privacy, age, confidence
   and unknown states for presence/attention; provide useful fresh frames for
   an object shown to Lamp. Include decoding, model-asset provisioning and the
   maintained inference dependency rather than stopping at a placeholder trait.
   Camera loss or darkness must not block hearing or fabricate visual knowledge.
2. Harden the evaluation relay with bounded incremental framing, UTF-8-safe
   limits, explicit oversized/truncated-line failures and child cleanup. Keep
   all attempts and original cached stimuli.
3. Extend evaluation to execute the actual production coordinator and worker
   graph with synthetic hardware ports and scripted Gemini. Test stalls,
   disconnects, mute/reopen, worker death/restart, blocked output, stale camera,
   motion inhibition and cancellation while audio continues. Retain the current
   fake-policy runner as an evaluator canary, with separate labels.

Deliver reusable process-level tests and a report tracing each behavior through
the real Rust code path. The 42 scenarios and 21 evaluator checks are starting
assets, not evidence that the robot already passes those scenarios.

## Working order and publication

- First two hours: agree concrete message types, timestamps, ownership, capacity,
  failure semantics and hardware-port interfaces. Keep audio/cancellation
  independent of inference, sensors, cloud waits and actuator work.
- By hour eight: publish small tested worker/component commits and the first
  real coordinator integration cases. Integrate continuously.
- By hour sixteen: exercise complete process paths, including failures and
  recovery. Fix functional gaps before expanding repeated simulation counts.
- Final eight hours: run stress/soak and source-only host/ARM checks on one
  frozen combined source. Record remaining implementation and physical gaps.

Each external agent commits exact files and pushes its own workstream branch,
then supplies SHA, diff scope, exact verification commands/results and remaining
gaps. Use single-line messages without trailers; never `git add -A`. The main
agent reviews and integrates into `lamp-v2-chat`. Avoid direct concurrent pushes
to the integration branch and preserve all workstream history.

## Meaning of implementation completion

All required paths must execute in the standalone runtime: microphone to
admission to Gemini to speaker; camera/perception to context/admission; controls
and configured sensors to fresh observations; and one choreographer to guarded
ring/head/body workers. No required behavior may exist only in a mock, prompt,
unconnected library, hidden Python/Go fallback or unprovisioned model stub.

Require formatting, strict workspace/all-target Clippy, applicable host tests,
Linux/ARM64 compilation and tests, release builds, source-only portability and
tests of the actual process graph under blocked dependencies. Preserve bounded
queues, workers, retries and diagnostic storage. Report skipped checks and
failed attempts. Missing code means implementation is still unfinished.

Physical validation follows: mic DSP/echo and human speakers, acoustic latency,
motor units/load/clearance, camera framing, visible light/motion timing, installed
sensor behavior and the owner's judgment of naturalness. Those remain part of
the full goal even after the code and offline gates pass.
