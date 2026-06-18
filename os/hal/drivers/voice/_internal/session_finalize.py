"""End-of-turn finalization for VoiceService._stream_session.

Combines the STT segments into one transcript and finalizes the captured PCM
buffer: snapshots the full buffer for SER, trims trailing silence from the
speaker-recognition copy (in place), and reports the turn's audio duration.
"""

import logging

from hal.drivers.voice._internal import config as voice_cfg

logger = logging.getLogger("hal.voice")


def finalize_session(audio_buffer, longest_partial, final_segments, last_speech_idx):
    """Return ``(combined_transcript, ser_audio_buffer, buf_duration_s)``.

    Mutates ``audio_buffer`` in place (trims trailing silence) — the caller's
    reference sees the trimmed buffer, used for speaker recognition. The returned
    ``ser_audio_buffer`` is an untrimmed snapshot kept for SER (laughter/sighs).
    """
    # Combine all final segments + any trailing partial into one transcript.
    if longest_partial[0]:
        final_segments.append(longest_partial[0])
    combined = " ".join(final_segments).strip()

    # Snapshot the FULL (untrimmed) buffer for SER before trimming.
    ser_audio_buffer = list(audio_buffer)

    # Remove trailing silence from audio_buffer for speaker recognition.
    # Leaves a 200ms tail for word endings; STT buffer unaffected.
    if last_speech_idx >= 0:
        tail_frames = int(200 / voice_cfg.FRAME_DURATION_MS) + 1
        trim_end = min(last_speech_idx + tail_frames + 1, len(audio_buffer))
        dropped = len(audio_buffer) - trim_end
        if dropped > 0:
            del audio_buffer[trim_end:]
            logger.info(
                "Session TRIM — dropped %d trailing-silence frames (~%.2fs) "
                "[speaker-recog buffer only; SER keeps full %d frames]",
                dropped,
                dropped * voice_cfg.FRAME_DURATION_MS / 1000,
                len(ser_audio_buffer),
            )

    # Final snapshot of the buffer for traceability before it goes out of scope.
    # 1 session = 1 speaking turn = this many frames.
    buf_frames = len(audio_buffer)
    buf_bytes = sum(len(b) for b in audio_buffer)
    buf_duration = buf_bytes / (voice_cfg.STT_RATE * 2)
    logger.info(
        "Session END — buffer frames=%d bytes=%d duration=%.2fs transcript=%r",
        buf_frames,
        buf_bytes,
        buf_duration,
        combined or "(empty)",
    )
    return combined, ser_audio_buffer, buf_duration
