"""Bounds how many images stay live in an OpenAI-style chat messages list.

Images ride inline on the turn that captured them — same message, same
position in history — so the shape stays ordinary chat semantics (a photo
shared mid-conversation). This only trims which of them survive: once more
than `max_images` are tracked, the oldest is stripped down to its text part
in place. The message object is not removed and its text is never touched —
only `_trim_history`'s own message-count window can do that.

    images = ImageContext(max_images=3)
    message = {"role": "user", "content": [...]}
    messages.append(message)
    images.track(message)          # call once, right after appending
    ...
    images.reconcile(messages)     # call after anything drops messages
"""


class ImageContext:
    def __init__(self, max_images: int):
        self._max_images = max(0, max_images)
        self._tracked: list[dict] = []  # oldest first; same objects as in messages

    def track(self, message: dict) -> None:
        """Register a message that was just appended with an image part.

        Evicts the oldest tracked image once the window is over capacity,
        stripping it to text-only IN PLACE — `message` is the same dict
        object living in the caller's messages list, so the edit is visible
        there without touching history length or trimming anything else.
        """
        self._tracked.append(message)
        while len(self._tracked) > self._max_images:
            self._strip_image(self._tracked.pop(0))

    def reconcile(self, messages: list[dict]) -> None:
        """Drop tracked entries no longer present in `messages`.

        Call after anything that removes messages by identity rather than
        through `track()` (history trimming, a context rebuild) — otherwise
        a dead reference counts against the window forever and the live
        images undercount by one.
        """
        if not self._tracked:
            return
        self._tracked = [m for m in self._tracked if any(m is x for x in messages)]

    def reset(self) -> None:
        """Drop all tracking — call when the messages list itself is replaced."""
        self._tracked.clear()

    @staticmethod
    def _strip_image(message: dict) -> None:
        content = message.get("content")
        if not isinstance(content, list):
            return
        text_parts = [p for p in content if isinstance(p, dict) and p.get("type") == "text"]
        if len(text_parts) == 1:
            message["content"] = text_parts[0].get("text", "")
        elif text_parts:
            message["content"] = text_parts
        else:
            message["content"] = ""
