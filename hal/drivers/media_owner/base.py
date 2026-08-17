"""Contract for handing device hardware between HAL and a vendor process.

Some bodies ship a runtime of their own that already holds the camera and the
audio PCMs when HAL starts. HAL cannot simply open them — the devices are busy —
and it must not kill the vendor process either, because on those bodies the same
process also owns motion. What it can do is ask for the hardware, use it, and
give it back.

A device declares that situation in ROBOT.md rather than HAL guessing it:

    audio:  { routes: [audio, speaker, voice], required: true, owner: pollen_daemon }

`owner:` names an implementation of this protocol, resolved through
media_owner/factory.py — the same shape as `driver:` on motion and vision.
Capabilities with no `owner:` are the normal case: HAL opens the hardware
directly and nothing here runs.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MediaOwner(Protocol):
    """A process HAL borrows camera and microphone from."""

    def release(self) -> bool:
        """Take the hardware from the owner. Called before HAL opens anything.

        Returns True when HAL may now open the devices. Implementations report
        failure rather than raising: HAL still has to boot, degraded, and an
        operator can repair it by hand.
        """
        ...

    def acquire(self) -> bool:
        """Give the hardware back, once HAL has closed its own handles."""
        ...
