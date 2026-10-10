# Resumption context provenance

## Context reports metadata provenance

`Context::Resumed` means the retained point was last advertised current and
Lamp has no known input accepted on a later connection than the one that
advertised it. It is not an acknowledgement that the service consumed every
client message or that the latest question, answer, or full conversation is
present in model context.

If connection A advertises a current handle, B resumes with that handle,
accepts a new exchange, and fails without issuing a new handle, the next
recovery reports `ResumedBeforeLatest`. B's lack of a metadata update cannot
renew A's current flag. An explicit invalidation on B also applies to the
inherited point, even if B never issued a local handle. The old handle is
preserved as the available resumption point; no input is replayed. Later idle
outages cannot clear known staleness. A newly advertised valid handle replaces
the retained point, with the original meaning of the service's current flag.

This correction uses source-session identity and known invalidation or later
input only. It intentionally does not infer a client-message watermark from a
request ID. In particular, Audio/End can follow an advertisement within the
same request. Exact coverage remains unknown without supported wire evidence.
Google documents that resuming with an earlier token can lose later data:
[Live API SessionResumptionUpdate](https://ai.google.dev/api/live#sessionresumptionupdate).
The Firebase interface documents an optional `lastConsumedClientMessageIndex`
for the included client-message boundary:
[LiveSessionResumptionUpdate](https://firebase.google.com/docs/reference/js/ai.livesessionresumptionupdate).
Endpoint support and transparent mode have not been probed here, and this
slice neither implements nor relies on that field. The source-session
qualification is Lamp's conservative engineering inference, not an assertion
that those APIs guarantee complete model memory.

## Scope and qualification

The correction changes only Gemini recovery/session state and its focused
tests. It adds one source-session stamp to a retained resumption point and one
bounded invalidation flag per connection. Actual handle bytes, setup defaults,
retry bounds, cancellation policy and input replay behavior remain unchanged.
No network request or wait is added to the ordinary conversation path.

The unchanged parent reproduced four failures among seven targeted cases; all
seven pass after the correction. They cover a reused handle after newer input,
an idle replacement, a fresh handle, invalidation with and without a local
handle, repeated outages, and a fresh advertisement after partial recovery.
Every reconnect verifies the handle and absence of replayed input.

The isolated Gemini verification passed 116 tests, strict all-target Clippy
and scoped formatting. Its one pre-existing long soak remained ignored; the
normal 60-turn fault soak passed. The first Clippy attempt rejected a boolean
expression, which was simplified before the final passing gates. No warning
suppression was added.

Root integration source contains 173 Rust/Cargo/toolchain files. Its manifest
SHA256 is `a19382a9fba8c21103104ff2f6f853a11dfa489eb64d4a684300ba4ad5f735aa`.
It also includes the diagnostic shutdown correction. See
[final combined qualification](provider-recovery-integration.md#final-combined-qualification)
for host and ARM verification. This note does not turn the parent checkpoint's
gates into a test result for this newer source.

No cloud endpoint, microphone, speaker, camera or physical Lamp was used.
Exact restored model memory and actual endpoint resumption behavior remain
unverified. Legacy runtime reporting is still diagnostic output and repeated
readiness; the coordinator's typed availability contract is separate work.
