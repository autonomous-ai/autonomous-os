"""ElevenLabs TTS backend with streaming support."""

import logging
from typing import Iterator, Optional

from hal.presets import LANG_EN, LANG_VI
from hal.drivers.voice.tts.backend import TTSBackend, STREAM_CHUNK_SIZE
from hal.drivers.voice.tts.openai import _ensure_openai_v1

logger = logging.getLogger("hal.voice.tts")


class ElevenLabsTTSBackend(TTSBackend):
    """ElevenLabs TTS backend with streaming support."""

    DEFAULT_MODEL = "eleven_v3"
    ELEVENLABS_PATH = "/elevenlabs"

    # Voice name -> voice_id mapping, grouped by trained language.
    # Curated for companion AI — warm, friendly, expressive. Top picks marked (*).
    #
    # eleven_v3 is multilingual — every voice can technically speak any
    # language — but voices trained on a language sound substantially more
    # natural in that language (accent, prosody). The web UI filters this
    # by stt_language so VN/CN owners don't have to scroll past 22 American
    # voices to find one that fits.
    #
    # zh-CN and zh-TW share the same voice pool — script (Simplified vs
    # Traditional) differs in TEXT, not in speaker. Jin has a Taiwan accent
    # which the web UI may want to surface for zh-TW pickers.
    # "zh" below is an internal meta-bucket (not a stt_language code) —
    # both LANG_ZH_CN and LANG_ZH_TW share this voice pool since
    # voice IDs are script-agnostic.
    _LANG_BUCKET_ZH = "zh"

    VOICE_IDS_BY_LANG = {
        LANG_EN: {
            # Female — premade
            "Rachel": "21m00Tcm4TlvDq8ikWAM",       # (*) warm, natural American
            "Sarah": "EXAVITQu4vr4xnSDxMaL",        # (*) friendly, clear American
            "Nicole": "piTKgcLEGmPE4e6mEKli",       # soft, inspirational
            # Female — community (conversational, young, American)
            "Terra": "aFueGIISJUmscc05ZNfD",         # (*) bubbly, friendly — 14k clones
            "Maria": "vZzlAds9NzvLsFSWp0qk",        # (*) soft, calm, expressive — 48k clones
            "Sophie": "AEW6JTgnyoPaoB9zlK3S",       # (*) sparky, energetic, young
            "Piper": "rzgrf9VyEb0LLa824k8Q",        # spirited, upbeat, dynamic
            "Mia": "052jzHJceQiZr7ltnY0C",          # lively, warm, expressive
            "Kimmy": "TmK7x2BFDD7TOVlR69J2",        # youthful, sweet, natural charm
            "Brianna": "2NzqTfQARqdn4tcBKTSh",      # soft, sincere, intimate
            "Ally": "qmm0vRXCIew16ilYAeiI",         # bubbly, fun, caring
            "Tori": "lAxf5ma5HGtzxC434SWT",         # confident, warm, encouraging
            # Male — premade
            "Brian": "nPczCjzI2devNBz1zQrb",        # (*) cheerful, relatable American
            "Adam": "pNInz6obpgDQGcFmaJgB",         # (*) warm, emotional depth
            "Daniel": "onwK4e9ZLuTAKqWW03F9",       # (*) well-paced, clear
            "George": "JBFqnCBsd6RMkjVDRZzb",
            "James": "ZQe5CZNOzWyzPSCn5a3c",        # calm British
            "Liam": "TX3LPaxmHKxFdv7VOQHJ",        # energetic American
            "Charlie": "IKne3meq5aSn9XLyUdCD",
            "Sam": "yoZ06aMxZJJ28mfd3POQ",
            # Male — community (conversational, young, American)
            "Sean": "FgARTjeugpFkVodK0Ovq",         # (*) casual, optimized for conversation — 1.9k clones
            "Kael": "RxsTyZQJnPygpas5IyzL",         # (*) energetic, trendy, youthful — 1.8k clones
            "Brooks": "sUzXYdokj3o9QQ91yPRF",       # (*) bright, affable, friendly smile — 1.5k clones
            "Erion": "BSgaLWMIhbNhOCIH1apf",        # unique, friendly, casual — 1.3k clones
        },
        LANG_VI: {
            "Ngan": "a3AkyqGG4v8Pg7SWQ0Y3",         # (*) Female, bubbly, friendly, authentic
            "Linh": "L5c6tGA8OiORYKxez5Zu",         # (*) Female, soft, calm, beautifully expressive
            "Huyen": "foH7s9fX31wFFH2yqrFa",        # Female, calm, friendly, clear (Da Nang)
            "Freya": "rXOGzMiqbmjugMpzKMEx",        # Female, young, soft, cute (Northern accent)
            "Nathan": "u8EWWYyBDfXFxHak7WM3",       # (*) Male, soft-spoken, gentle, sing-song Central
            "Quan": "puBBfOSRT9Dbk3FUJQGd",         # Male, warm, thoughtful Central tone
        },
        _LANG_BUCKET_ZH: {
            "Amy": "bhJUNIXWQQ94l8eI2VUf",          # (*) Female, relaxed, friendly Beijing tone
            "Sage": "APSIkVZudNbPAwyPoeVO",         # (*) Female, warm, soothing narrative voice
            "Xiaoxi": "9DMBSOAnMDPiFAsz1ZGK",       # Female, neutral, friendly, approachable
            "Yun": "YxbjaPemDJV2xlfvkiIG",          # Female, elegant, sweet, gentle
            "Evan Zhao": "MI36FIkp9wRP7cpWKPTl",    # (*) Male, calm, trustworthy, warm
            "Jin": "vZZLclMx4wouUtKBRfZn",          # Male, casual, Taiwan-influenced (good for zh-TW)
        },
    }

    # Flat name -> voice_id lookup, derived from VOICE_IDS_BY_LANG. Keeps
    # the resolve path self.VOICE_IDS.get(voice, voice) cheap and unaware
    # of language, so any saved tts_voice still resolves regardless of
    # which language the owner is currently set to.
    VOICE_IDS = {
        name: vid
        for lang_voices in VOICE_IDS_BY_LANG.values()
        for name, vid in lang_voices.items()
    }

    @classmethod
    def voices_for_language(cls, lang: str) -> list:
        """Return curated voice names for a given stt_language code.
        Empty / unknown lang → flat list of all voices (so the picker
        keeps working when stt_language is "auto"). zh-CN and zh-TW
        both map to the shared "zh" bucket since voice IDs are
        script-agnostic."""
        if not lang:
            return list(cls.VOICE_IDS.keys())
        bucket = LANG_EN
        if lang.startswith(LANG_VI):
            bucket = LANG_VI
        elif lang.startswith(cls._LANG_BUCKET_ZH):
            bucket = cls._LANG_BUCKET_ZH
        elif lang.startswith(LANG_EN):
            bucket = LANG_EN
        pool = cls.VOICE_IDS_BY_LANG.get(bucket)
        if not pool:
            pool = cls.VOICE_IDS_BY_LANG[LANG_EN]
        return list(pool.keys())

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        self._api_key = api_key
        self._base_url = _ensure_openai_v1(base_url or "") + self.ELEVENLABS_PATH
        self._client = None
        try:
            import httpx
            # Persistent client reuses TCP/TLS across speaks -- saves ~100-500ms per
            # call vs httpx.stream() module-level which builds a fresh Client+TLS each time.
            self._client = httpx.Client(
                timeout=30.0,
                limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300.0),
            )
            logger.info("ElevenLabs TTS backend ready (proxy=%s)", self._base_url)
        except ImportError as e:
            logger.warning("httpx not available for ElevenLabs backend: %s", e)

    @property
    def available(self) -> bool:
        return self._client is not None and bool(self._api_key)

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    @property
    def volume_boost(self) -> float:
        return 1.0

    def stream_pcm(
        self,
        text: str,
        voice: str,
        model: str,
        speed: float,
        instructions: Optional[str] = None,
    ) -> Iterator[bytes]:
        el_model = model if model.startswith("eleven_") else self.DEFAULT_MODEL
        # Resolve voice name to voice_id (pass through if already an ID)
        voice_id = self.VOICE_IDS.get(voice, voice)
        # output_format is a query param, not body — pcm_24000 = 24kHz 16-bit mono
        url = f"{self._base_url}/text-to-speech/{voice_id}/stream?output_format=pcm_24000"
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        body = {
            "text": text,
            "model_id": el_model,
        }
        if speed != 1.0:
            body["voice_settings"] = {"speed": max(0.7, min(1.2, speed))}

        with self._client.stream(
            "POST", url, headers=headers, json=body
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(STREAM_CHUNK_SIZE):
                yield chunk
