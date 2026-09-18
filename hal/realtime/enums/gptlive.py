"""GPT-Live (OpenAI /v1/live) specific enumerations."""

from enum import StrEnum


class GPTLiveVoice(StrEnum):
    """Live voices accepted by gpt-live-1 (BFF integration doc, verified on the
    real model 2026-09-17). The SDK's `BuiltInVoice` literal is wider — it also
    carries Realtime-only names (alloy, ash, …) that a Live `session.start`
    rejects, which would kill the session before the first word. `marin` is the
    default; `bossa` / `tempo` are Portuguese, the rest English with regional
    accents."""

    MARIN = "marin"
    QUARTZ = "quartz"
    RIPPLE = "ripple"
    VESPER = "vesper"
    WILLOW = "willow"
    STONE = "stone"
    GLEAM = "gleam"
    MERIDIAN = "meridian"
    BOSSA = "bossa"
    TEMPO = "tempo"
    BEACON = "beacon"
    DELTA = "delta"
    CINDER = "cinder"
