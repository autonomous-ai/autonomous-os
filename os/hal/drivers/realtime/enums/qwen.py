"""Qwen Omni Realtime-specific enumerations."""

from enum import StrEnum


class QwenVoice(StrEnum):
    # Union of the realtime voice sets (Model Studio realtime docs). Support is
    # per-model (probed 2026-07-06): qwen3.5-omni-plus-realtime accepts only
    # Serena/Ethan; qwen-omni-turbo-realtime accepts all four. A wrong pairing
    # fails loudly with InvalidParameter on the first response.
    CHERRY = "Cherry"
    SERENA = "Serena"
    ETHAN = "Ethan"
    CHELSIE = "Chelsie"
