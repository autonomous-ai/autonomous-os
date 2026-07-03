"""Filter Gemini Live chain-of-thought leaks out of the reply text.

gemini-3.1-flash-live-preview cannot have thinking disabled: thinking_level=
MINIMAL and thinking_budget=0 are both accepted but ignored (measured
thoughts_token_count=125-168 on reasoning turns with every config, device
2026-07-03). Usually the thoughts stay internal, but on tool/vision/grounding
turns in long sessions the server sometimes streams the model's whole TEXT
channel — English planning ("The user is insisting...", "Phrasing draft:",
"Delivery guidance:") plus the real answer — into output_audio_transcription,
while the model's own AUDIO carries only the clean answer (out_audio ~217tok vs
1000+ chars of transcription on the leaked turns). With native audio off, HAL
speaks the transcription → the leak is read aloud and forwarded as [REPLY].

No server-side knob fixes this, so filter it client-side at sentence
granularity. Two tiers:

- STRONG markers — meta-discourse that can never occur in a reply spoken TO the
  user (third-person "the user", planning labels straight from the leak corpus).
  Always dropped; entering CoT mode for the rest of the turn.
- English continuation — once CoT mode is on and the device reply language is
  not English, unmarked English planning sentences ("Say clearly that it
  didn't decrease.") are dropped too. The real answer (non-English) passes.
  On English-language devices only the STRONG tier applies, so a legit English
  reply is never at risk from the heuristics.
"""

import logging
import re

logger = logging.getLogger("hal.voice")

# Meta-discourse markers. A reply addressed to the user never refers to them in
# the third person and never carries planning labels. Taken verbatim from the
# observed leak corpus (device lamp-ac82, 2026-07-03).
_STRONG = re.compile(
    r"(?i)(?:\bthe users?\b|\bthe speaker\b"
    r"|phrasing draft|delivery guidance|spoken delivery"
    r"|\baudio tags?\b|\bemotion tool\b|via tool call"
    r"|\bsystem prompt\b|language lock|\bpersona\b"
    r"|\bmy search results?\b|\bsearch results? (?:show|suggest|indicate)\b)"
)

# Sentence-initial planning openers. Only meaningful when the reply language is
# NOT English (a Vietnamese-locked session never legitimately starts a sentence
# with these); on English devices they could open a genuine reply.
_OPENER = re.compile(
    r"(?i)^\s*(?:users? (?:want|wants|is|are|asked|insists?)\b"
    r"|i (?:need|should|must|will|can(?:not|'t)?) \b"
    r"|therefore,? i\b|looking at the\b|re-examining\b|plan:\s*$)"
)

# Split on sentence enders (keep multi-line structure: newlines split too).
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？:])\s+|\n+")

# Letters outside ASCII (Vietnamese diacritics, CJK, ...) mean "not English".
_NON_ASCII_LETTER = re.compile(r"[^\x00-\x7f]")
# Leading audio/emotion tags like "[caring] " don't decide the language.
_LEADING_TAGS = re.compile(r"^(?:\s*\[[^\]]{1,30}\])+\s*")

_QUOTES = "\"'“”‘’«»"


def _word_set(sentence: str) -> frozenset[str]:
    return frozenset(re.findall(r"\w+", sentence.lower()))


def _looks_english(sentence: str) -> bool:
    s = _LEADING_TAGS.sub("", sentence).strip()
    letters = re.findall(r"[^\W\d_]", s)
    if not letters:
        return False
    # Ratio, not any(): "non-cliché" has one é but is English planning text,
    # while real Vietnamese runs ~30%+ diacritics.
    non_ascii = sum(1 for ch in letters if ord(ch) > 127)
    if non_ascii / len(letters) > 0.05:
        return False
    # Require a few words so bare interjections ("OK!", "Ha ha") survive.
    return len(re.findall(r"[A-Za-z]{2,}", s)) >= 3


class CoTLeakFilter:
    """Per-turn stateful filter. Feed it reply text in arrival order."""

    def __init__(self, reply_language: str) -> None:
        # "" (unset) is treated as English → only the STRONG tier applies.
        lang = (reply_language or "").strip().lower()
        self._non_english: bool = bool(lang) and not lang.startswith("english")
        self._cot_mode: bool = False
        self._seen: list[frozenset[str]] = []
        self.dropped: int = 0

    def _is_leak(self, sentence: str) -> bool:
        s = sentence.strip()
        if not s:
            return False
        if _STRONG.search(s):
            self._cot_mode = True
            return True
        if self._non_english and _OPENER.match(s):
            self._cot_mode = True
            return True
        if self._cot_mode:
            if self._non_english and _looks_english(s):
                return True
            # Leaked turns carry the answer as a QUOTED phrasing draft before the
            # real thing ('Phrasing draft: "Dạ anh Leo ơi, ..."'). The label was
            # dropped above; drop the quoted draft too (leading OR dangling
            # quote = part of it) or the answer is spoken twice. Same for
            # near-duplicates of a sentence already kept this turn — the drafts
            # differ from the final answer by a word or two, so the dedup is
            # fuzzy (word-set overlap), not exact.
            if s[0] in _QUOTES or s[-1] in _QUOTES:
                return True
            # Bare plan fragments ("1.", "4.", "Stop.", "thinking.") — ASCII
            # runts that survived the label drops around them. Pure audio tags
            # ("[confused]") are exempt: they steer TTS delivery, not content.
            bare = _LEADING_TAGS.sub("", s).strip()
            if bare and not _NON_ASCII_LETTER.search(bare) and len(_word_set(bare)) <= 1:
                return True
            words = _word_set(s)
            if words and any(
                len(words & seen) / len(words | seen) >= 0.7
                for seen in self._seen
            ):
                return True
        return False

    def filter_text(self, text: str) -> str:
        """Drop CoT sentences from `text`, keeping the rest in order."""
        if not text:
            return text
        kept: list[str] = []
        for sentence in _SENTENCE_SPLIT.split(text):
            if self._is_leak(sentence):
                self.dropped += 1
                logger.warning(
                    "[realtime] CoT leak dropped (not spoken/forwarded): %r",
                    sentence.strip()[:120],
                )
            elif sentence.strip():
                s = sentence.strip()
                kept.append(s)
                words = _word_set(s)
                if words:
                    self._seen.append(words)
        return " ".join(kept)


def clean_transcript(text: str, reply_language: str) -> str:
    """Filter a full turn transcript with fresh state (for [REPLY]/memory)."""
    return CoTLeakFilter(reply_language).filter_text(text)
