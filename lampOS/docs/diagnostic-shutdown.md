# Preserve the diagnostic recording tail at shutdown

The first integrated ARM64 Linux suite reported zero retained capture frames
where the diagnostic test expected 160. The recording correctly remained
invalid and had no completion marker. Reviewing that failure exposed a
pre-existing race in the optional diagnostic writer, separate from the
provider's answer-delivery path.

The writer could observe an empty queue, then the producer could enqueue
accepted privacy, PCM and End records and close. The writer saw the new closed
state and exited using its earlier empty result. The same ordering was possible
with producer abandonment or a terminal producer fault. It could omit accepted
recording data during either normal completion or a fault.

The writer now performs a fresh pop after observing a terminal state before
it treats an empty queue as EOF. It still validates every record before writing,
respects duration/byte limits, and preserves the bounded finish acknowledgement.
Capture submissions remain nonblocking. The pinned rtrb queue synchronizes its
producer-drop observation; the shared closure/fault flags use acquire/release
ordering. No timeout, queue or privacy bound is widened.

## Deterministic evidence

A test-only latch pauses the writer immediately after its first empty pop.
The producer then queues the accepted records and closes or latches its fault
before releasing the writer. Four Completed/Fault × closed/abandoned schedules
reproduced three queued records with zero written, zero capture frames and no
End. A fifth producer-fault schedule reproduced two accepted records with zero
written frames. These controlled failures prove the source race. The original
ARM failure did not record its exact interleaving, so it is not assigned a more
precise cause than the evidence supports.

All five schedules pass after the correction. Normal completion retains 160
frames and all three records and can publish a valid completion marker. An
End(Fault) retains the same data, records AUDIO_FAULT, remains invalid and
publishes no marker. A producer rejection preserves the accepted two-record
prefix while remaining incomplete and invalid. The existing integration case
also now asserts that both privacy and capture submissions were accepted.

The isolated final verification passed 10 diagnostics unit tests, 18 diagnostics
integration tests, scoped formatting and strict all-target lamp-live Clippy.
All before/after sources, logs and hashes are retained. The final combined
source and host/ARM results are recorded in
[provider recovery integration](provider-recovery-integration.md).

This repair concerns diagnostic evidence files. It is not a measured acoustic
latency improvement or a claim that microphone, speaker, echo cancellation or
physical conversation behavior is qualified.
