"""Curated Piper voice catalogue — the list the device offers for download.

Every entry here has had its dataset licence read and recorded. That check is
the point of this file: the upstream Piper catalogue is ~175 voices and a
meaningful share of them are CC BY-NC-SA or a manually-granted research
licence, which cannot ship in a product that is sold. Adding a voice to this
list is a licensing decision, not a taste one.

Verified excluded (do not re-add without new evidence):
  en_US-lessac      Blizzard 2013 — per-organisation research licence, granted
                    by hand after registration. Not commercial.
  en_US-hfc_female  CC BY-NC-SA 4.0
  en_US-hfc_male    CC BY-NC-SA 4.0
  en_US-ryan        CC BY-NC-SA 4.0
  hi_IN-priyamvada  CC BY-NC-SA 4.0
  vi_VN-vivos       CC BY-NC-SA 4.0
  vi_VN-25hours     licence unknown
  ru_RU-irina       licence unknown
  zh_CN-huayan      licence unknown

ATTRIBUTION: entries marked requires_attribution must be credited in
CREDITS.md before shipping. CC0 and public-domain entries need nothing.
"""

# Where the models come from. Pinned to the release layout Piper publishes.
VOICES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# The Piper binary itself: a tarball of the executable plus its espeak-ng and
# phonemize shared libraries. Small (~26 MB) compared to any single voice.
BINARY_URL = (
    "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/"
    "piper_linux_aarch64.tar.gz"
)

# name → metadata. `path` is the directory under VOICES_BASE holding
# <name>.onnx and <name>.onnx.json.
CATALOG = {
    "en_US-ljspeech-medium": {
        "language": "English (US)",
        "lang_code": "en",
        "path": "en/en_US/ljspeech/medium",
        "license": "public domain",
        "requires_attribution": False,
        "size_mb": 63,
    },
    "en_US-kristin-medium": {
        "language": "English (US) — alt voice",
        "lang_code": "en",
        "path": "en/en_US/kristin/medium",
        "license": "public domain",
        "requires_attribution": False,
        "size_mb": 63,
    },
    "en_US-libritts_r-medium": {
        "language": "English (US) — multi-speaker",
        "lang_code": "en",
        "path": "en/en_US/libritts_r/medium",
        "license": "CC BY 4.0",
        "requires_attribution": True,
        "size_mb": 63,
    },
    "vi_VN-vais1000-medium": {
        "language": "Tiếng Việt",
        "lang_code": "vi",
        "path": "vi/vi_VN/vais1000/medium",
        "license": "CC BY 4.0",
        "requires_attribution": True,
        "size_mb": 63,
    },
    "es_ES-davefx-medium": {
        "language": "Español",
        "lang_code": "es",
        "path": "es/es_ES/davefx/medium",
        "license": "CC0",
        "requires_attribution": False,
        "size_mb": 63,
    },
    "de_DE-thorsten-medium": {
        "language": "Deutsch",
        "lang_code": "de",
        "path": "de/de_DE/thorsten/medium",
        "license": "CC0",
        "requires_attribution": False,
        "size_mb": 63,
    },
    "pt_BR-faber-medium": {
        "language": "Português (Brasil)",
        "lang_code": "pt",
        "path": "pt/pt_BR/faber/medium",
        "license": "CC0",
        "requires_attribution": False,
        "size_mb": 63,
    },
    "fr_FR-siwis-medium": {
        "language": "Français",
        "lang_code": "fr",
        "path": "fr/fr_FR/siwis/medium",
        "license": "CC BY 4.0",
        "requires_attribution": True,
        "size_mb": 63,
    },
}

# The voice a device installs when the operator turns Piper on without picking
# one. Public domain, so shipping it carries no obligation at all.
DEFAULT_VOICE = "en_US-ljspeech-medium"


def voice_urls(name: str):
    """(onnx_url, json_url) for a catalogue entry, or None if not listed.

    Downloads are restricted to catalogue entries on purpose: the name reaches
    here from the admin UI, and building a URL from arbitrary input would let a
    caller pull any file from the host into /opt/piper.
    """
    meta = CATALOG.get(name)
    if not meta:
        return None
    base = f"{VOICES_BASE}/{meta['path']}/{name}"
    return f"{base}.onnx", f"{base}.onnx.json"
