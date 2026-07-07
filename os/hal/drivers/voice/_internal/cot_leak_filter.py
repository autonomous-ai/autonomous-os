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
speaks the transcription → the leak is read aloud (burning TTS characters) and
forwarded as [REPLY], where it re-enters context and self-reinforces.

No server-side knob fixes this, so filter it client-side at sentence
granularity, in three tiers:

- TRIGGER markers — meta-discourse that can never occur in a reply spoken TO
  the user: third-person "the user is/wants/..." and planning labels straight
  from the leak corpus. Always dropped; they switch the turn into CoT mode.
- SECONDARY markers — meta terms ("persona", "system prompt", "emotion tool")
  that a reply could legitimately contain (this fleet's users are developers
  who ask the device about itself). Dropped only once CoT mode is already on.
- CoT-mode continuation — the leak keeps planning in English and repeats the
  answer as quoted drafts. Once triggered: English-looking sentences (only when
  the device reply language is not English), quoted draft fragments, bare plan
  runts ("1.", "Stop."), and fuzzy near-duplicates of already-kept sentences
  are dropped too. The real answer passes.

Language handling: the model's CoT is ALWAYS English, whatever the session
language. For non-Latin-script languages (Vietnamese diacritics, Chinese,
Japanese, Korean, Thai) an ASCII-only sentence in CoT mode is safely English.
For Latin-script languages (French, German, Indonesian, ...) that signal would
eat the real answer, so English detection additionally requires English
function words. On English devices only the marker tiers apply — a legitimate
English reply is never at risk from the heuristics.

NOTE: the main-agent path (openclaw/hermes replies spoken via os-server) has a
Go port of this filter in
os/services/server/agent/delivery/http/cot_leak_filter.go — keep the two in
sync when hardening either side.
"""

import logging
import re

logger = logging.getLogger("hal.voice")

# --- Marker tiers -----------------------------------------------------------

# TRIGGER: unambiguous CoT. "the user/the speaker" is verb-bound so that a
# legitimate reply mentioning "the user manual" / "the user interface" does not
# trip it; the leak corpus is always "The user is/wants/insists...".
_TRIGGER = re.compile(
    r"(?i)(?:\bthe (?:user|speaker)s? (?:is|are|was|were|wants?|wanted|asks?"
    r"|asked|insists?|insisted|seems?|seemed|says?|said|repeats?|repeated"
    r"|claims?|claimed|mentions?|mentioned|requests?|requested|greets?|greeted"
    r"|needs?|needed)\b"
    r"|phrasing draft|delivery guidance|spoken delivery)"
)

# SECONDARY: meta terms a legit reply could contain (dev users ask the device
# about itself) — only dropped once CoT mode is already on.
_SECONDARY = re.compile(
    r"(?i)(?:\bpersonas?\b|\bsystem prompts?\b|language lock|\baudio tags?\b"
    r"|\bemotion tool\b|via tool call"
    r"|\bmy search results?\b|\bsearch results? (?:show|suggest|indicate)\b"
    r"|\bsearch quer(?:y|ies)\b"
    r"|\b\d+ (?:sentences?|words)\b)"
)

# All-ASCII label sentence ending in a colon ("Vietnamese Translation:",
# "Length Check:", "Context:") — planning scaffolding from the leak corpus.
# Only meaningful when the reply language is NOT English: a real non-English
# reply never yields a pure-ASCII colon-terminated sentence.
_LABEL = re.compile(r"^[\x20-\x7e]{1,40}:$")

# Sentence-initial planning openers ("User wants...", "I need to...",
# "Looking at the latest image..."). Only meaningful when the reply language is
# NOT English — an English reply can legitimately open with these.
_OPENER = re.compile(
    r"(?i)^\s*(?:users? (?:want|wants|is|are|asked|insists?)\b"
    r"|i (?:need|should|must|will|can(?:not|'t)?) \b"
    r"|therefore,? i\b|looking at the\b|re-examining\b|plan:\s*$)"
)

# --- Text mechanics ---------------------------------------------------------

# Sentence enders (ASCII + fullwidth) and newlines; colons split so planning
# labels ("Phrasing draft: ...") separate from their payload. Also split when
# an ender is glued straight onto the next sentence ("Finalize response.Khả
# năng...") — the leak omits the space, and without the split the label drags
# the real answer down with it. Uppercase/non-ASCII lookahead keeps decimals
# ("3.5") and lowercase glue ("Node.js") intact.
_SENTENCE_SPLIT = re.compile(
    r"(?<=[.!?。！？：；:])\s+|\n+|(?<=[.!?])(?=[A-Z]|[^\x00-\x7f])"
)

_QUOTES = "\"'“”‘’«»「」『』【】＂＇"

# Quoted spans inside a sentence don't decide its language: the leak corpus
# has English planning sentences that embed the reply language in quotes
# ("The search query 'cách dùng select trong itron os' didn't yield...") —
# with the quoted Vietnamese counted, the non-ASCII ratio calls the whole
# sentence non-English and the CoT line is spoken. Straight single quotes
# need the word-boundary guards so contractions ("didn't") can't open a span.
_QUOTED_SPAN = re.compile(
    r"\"[^\"]*\"|“[^”]*”|‘[^’]*’|«[^»]*»|「[^」]*」|『[^』]*』"
    r"|(?<!\w)'[^']*'(?!\w)"
)

_NON_ASCII_LETTER = re.compile(r"[^\x00-\x7f]")
# Leading audio/emotion tags like "[caring] " don't decide the language.
_LEADING_TAGS = re.compile(r"^(?:\s*\[[^\]]{1,30}\])+\s*")

# CJK/Hangul/Kana have no spaces — tokenize per character so the fuzzy dedup
# works for Chinese/Japanese/Korean; everything else tokenizes per word.
_CJK_RANGE = "぀-ヿ㐀-䶿一-鿿豈-﫿가-힯"
_TOKEN = re.compile(rf"[{_CJK_RANGE}]|[0-9]+|[^\W\d_{_CJK_RANGE}]+")

# Languages whose real answers are dense in non-ASCII letters — there, any
# ASCII-only multi-word sentence inside CoT mode is safely English planning.
_NON_ASCII_SCRIPTS = ("vietnamese", "chinese", "japanese", "korean", "thai")

# For Latin-script non-English languages (French, German, Indonesian, ...) the
# ASCII signal would eat the real answer, so require English function words.
_EN_STOPWORDS = frozenset(
    "the a an is are was were to of and or that this it its i you we they"
    " will would should must need in on at with for be as by from since".split()
)


def _word_set(sentence: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall(sentence.lower()))


class CoTLeakFilter:
    """Per-turn stateful filter. Feed it reply text in arrival order."""

    def __init__(self, reply_language: str) -> None:
        # "" (unset) is treated as English → only the marker tiers apply.
        lang = (reply_language or "").strip().lower()
        self._non_english: bool = bool(lang) and not lang.startswith("english")
        self._non_ascii_script: bool = lang.startswith(_NON_ASCII_SCRIPTS)
        self._cot_mode: bool = False
        self._seen: list[frozenset[str]] = []
        self.dropped: int = 0

    def _looks_english(self, sentence: str) -> bool:
        s = _LEADING_TAGS.sub("", sentence).strip()
        # Judge the sentence by its own voice, not by what it quotes (see
        # _QUOTED_SPAN): quoted reply-language text inside an English planning
        # sentence must not rescue it.
        s = _QUOTED_SPAN.sub(" ", s)
        letters = re.findall(r"[^\W\d_]", s)
        if not letters:
            return False
        # Ratio, not any(): "non-cliché" has one é but is English planning
        # text, while real Vietnamese runs ~30%+ diacritics and CJK ~100%.
        non_ascii = sum(1 for ch in letters if ord(ch) > 127)
        if non_ascii / len(letters) > 0.05:
            return False
        words = re.findall(r"[A-Za-z]+", s)
        if len(words) < 3:
            # Bare interjections ("OK!", "Ha ha") survive.
            return False
        if self._non_ascii_script:
            return True
        # Latin-script language: an ASCII sentence may BE the answer — call it
        # English only when English function words anchor it.
        return sum(1 for w in words if w.lower() in _EN_STOPWORDS) >= 2

    def _is_leak(self, sentence: str) -> bool:
        s = sentence.strip()
        if not s:
            return False
        if _TRIGGER.search(s):
            self._cot_mode = True
            return True
        if self._non_english and _OPENER.match(s):
            self._cot_mode = True
            return True
        if self._non_english and _LABEL.match(s):
            self._cot_mode = True
            return True
        if not self._cot_mode:
            return False
        if _SECONDARY.search(s):
            return True
        if self._non_english and self._looks_english(s):
            return True
        # Leaked turns carry the answer as a QUOTED phrasing draft before the
        # real thing ('Phrasing draft: "Dạ anh Leo ơi, ..."'). The label was
        # dropped above; drop the quoted draft too (leading OR dangling quote =
        # part of it) or the answer is spoken twice.
        if s[0] in _QUOTES or s[-1] in _QUOTES:
            return True
        # Bare plan fragments ("1.", "Stop.", "Finalize response.", "Length
        # Check:") — ASCII runts up to two tokens that survived the label drops
        # around them; the English gate needs three words so two-word labels
        # slip between the two checks otherwise. Pure audio tags ("[confused]")
        # are exempt: they steer TTS delivery, not content.
        bare = _LEADING_TAGS.sub("", s).strip()
        if bare and not _NON_ASCII_LETTER.search(bare) and len(_word_set(bare)) <= 2:
            return True
        # Fuzzy near-duplicate of a sentence already kept this turn — drafts
        # differ from the final answer by a word or two, so exact dedup misses.
        words = _word_set(s)
        if words and any(
            len(words & seen) / len(words | seen) >= 0.7 for seen in self._seen
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
