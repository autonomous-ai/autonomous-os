"""End-of-turn finalization for VoiceService._stream_session.

Combines the STT segments into one transcript and finalizes the captured PCM
buffer: snapshots the full buffer for SER, trims trailing silence from the
speaker-recognition copy (in place), and reports the turn's audio duration.
"""

import logging

from hal.drivers.voice._internal import config as voice_cfg

logger = logging.getLogger("hal.voice")

# A one-word prefix match only convicts if the word is long enough to be
# evidence. Short function words appear in every reply ever spoken.
_MIN_SOLO_ECHO_WORD = 4


def _words(text):
    """Lowercased alphanumeric tokens — punctuation and case carry no signal."""
    return [
        "".join(c for c in token if c.isalnum())
        for token in text.lower().split()
        if any(c.isalnum() for c in token)
    ]


def strip_echo_prefix(transcript: str, spoken: str) -> str:
    """Drop a leading run of `transcript` that the device itself just said.

    A barge-in transcript is a MIXTURE, which is why the existing
    whole-transcript filter (sensing_sender.is_echo) cannot handle it: the
    pre-roll starts before the user did, so the reply's last words sit in front
    of theirs. Dropping the whole thing would discard the user's turn; keeping
    it sends the device its own words back. Device-observed 27/08/2026:
    "If you can, just go ahead and eat it." arrived as
    'situation. I mean, should the fine be heavy?' with 'situation' belonging
    to the reply that was cut off.

    Only a PREFIX is ever removed, and only one that appears verbatim in what
    was spoken. A single short word is not enough evidence — "the" appears in
    every reply — so a one-token match must be a long word.
    """
    if not transcript or not spoken:
        return transcript
    said = _words(spoken)
    heard = _words(transcript)
    if not said or not heard:
        return transcript

    matched = 0
    for count in range(min(len(heard), len(said)), 0, -1):
        prefix = heard[:count]
        if any(
            said[i: i + count] == prefix for i in range(len(said) - count + 1)
        ):
            matched = count
            break
    if matched == 0:
        return transcript
    if matched == 1 and len(heard[0]) < _MIN_SOLO_ECHO_WORD:
        return transcript
    if matched == len(heard):
        # Entirely the device's own voice. Leave it whole so the existing
        # similarity filter makes that call and logs it as the echo it is.
        return transcript

    # Walk the same number of tokens through the ORIGINAL text, so what is kept
    # keeps its punctuation and capitalisation for STT-facing consumers.
    seen, cut, in_token = 0, len(transcript), False
    for i, char in enumerate(transcript):
        if char.isalnum():
            in_token = True
        elif in_token:
            in_token = False
            seen += 1
            if seen == matched:
                cut = i
                break
    kept = transcript[cut:].lstrip(" .,!?;:—-").strip()
    if not kept:
        return transcript
    logger.info(
        "Echo prefix stripped (%d word(s)): %r → %r [device said %r]",
        matched, transcript[:60], kept[:60], spoken[-60:],
    )
    return kept


def finalize_session(
    audio_buffer, last_partial, final_segments, last_speech_idx, spoken_text="",
):
    """Return ``(combined_transcript, ser_audio_buffer, buf_duration_s)``.

    Mutates ``audio_buffer`` in place (trims trailing silence) — the caller's
    reference sees the trimmed buffer, used for speaker recognition. The returned
    ``ser_audio_buffer`` is an untrimmed snapshot kept for SER (laughter/sighs).

    ``spoken_text`` is what the device last said; a leading run of the
    transcript that repeats it is dropped (see strip_echo_prefix).
    """
    # Combine all final segments
    if last_partial[0]:
        final_segments.append(last_partial[0])
    combined = " ".join(final_segments).strip()
    combined = strip_echo_prefix(combined, spoken_text)

    # An STT final such as "." or "…" is not a user turn.  It used to pass
    # every `if combined` gate, which could send an empty-looking `voice_followup`
    # to the main agent and refresh the follow-up focus window.  Keep Unicode
    # letters and numbers (including Vietnamese and CJK text); reject only a
    # transcript made entirely of punctuation, symbols, or whitespace.
    if combined and not any(char.isalnum() for char in combined):
        logger.info(
            "Session transcript has no spoken content; treating it as empty: %r",
            combined,
        )
        combined = ""

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
