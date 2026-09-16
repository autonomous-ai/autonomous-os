"""GPT-Live (OpenAI /v1/live) specific enumerations."""

from enum import StrEnum


class GPTLiveVoice(StrEnum):
    """Built-in Live voices — the `BuiltInVoice` literal of openai 3.14.1
    (`openai.types.live`). The default for a session is `marin`."""

    ALLOY = "alloy"
    ASH = "ash"
    BALLAD = "ballad"
    BEACON = "beacon"
    BOSSA = "bossa"
    CEDAR = "cedar"
    CINDER = "cinder"
    CORAL = "coral"
    DELTA = "delta"
    ECHO = "echo"
    GLEAM = "gleam"
    MARIN = "marin"
    MERIDIAN = "meridian"
    QUARTZ = "quartz"
    RIPPLE = "ripple"
    SAGE = "sage"
    SHIMMER = "shimmer"
    STONE = "stone"
    TEMPO = "tempo"
    VERSE = "verse"
    VESPER = "vesper"
    WILLOW = "willow"
