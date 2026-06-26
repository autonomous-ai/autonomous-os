"""Base context manager — shared logic for summarization, turn management, and prompt assembly.

Subclasses implement agent-specific context loading (identity files, device memory, skill catalog).
"""

import json
import logging
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import hal.config as app_config
from hal.drivers.realtime.constants import RESOURCES_DIR
from hal.drivers.realtime.summarizer import RealtimeSummarizer

logger = logging.getLogger(__name__)


class ContextManagerBase(ABC):
    """Abstract base for realtime voice agent context managers.

    Concrete subclasses (OpenClawContextManager, HermesContextManager) implement
    the four abstract methods to load agent-specific context. The base class handles
    summarization, turn persistence, prompt assembly, and memory trimming.
    """

    DEFAULT_PROMPT_PATH: Path = RESOURCES_DIR / "system_prompt.md"
    PROVIDER_PROMPT_PATHS: dict[str, Path] = {
        "openai": RESOURCES_DIR / "system_prompt_openai.md",
        "gemini": RESOURCES_DIR / "system_prompt_gemini.md",
    }

    LANGUAGE_NAMES: dict[str, str] = {
        "en": "English",
        "vi": "Vietnamese",
        "zh-CN": "Chinese (Simplified)",
        "zh-TW": "Chinese (Traditional)",
        "ko": "Korean",
        "ja": "Japanese",
        "fr": "French",
        "de": "German",
        "es": "Spanish",
        "pt": "Portuguese",
        "id": "Indonesian",
        "th": "Thai",
    }

    def __init__(
        self,
        workspace_dir: str = "",
        realtime_memory_path: str = app_config.REALTIME_MEMORY_PATH,
        language: str | None = None,
        provider: str = "",
        max_memory_entries: int = app_config.REALTIME_MAX_MEMORY_ENTRIES,
        trim_keep: int = app_config.REALTIME_MEMORY_TRIM_KEEP,
        device_memory_max_chars: int = app_config.REALTIME_DEVICE_MEMORY_MAX_CHARS,
        realtime_memory_max_chars: int = app_config.REALTIME_MEMORY_MAX_CHARS,
        summarizer: RealtimeSummarizer | None = None,
    ) -> None:
        self._workspace: Path = Path(workspace_dir)
        self._realtime_memory_path: Path = Path(realtime_memory_path)
        self._language: str = language or "English"
        self._provider: str = provider.strip().lower()
        self._max_memory_entries: int = max_memory_entries
        self._trim_keep: int = trim_keep
        self._device_memory_max_chars: int = device_memory_max_chars
        self._realtime_memory_max_chars: int = realtime_memory_max_chars
        self._summarizer: RealtimeSummarizer | None = summarizer
        # Summary files
        self._summary_path: Path = self._realtime_memory_path.parent / "summary.md"
        self._device_summary_path: Path = (
            self._realtime_memory_path.parent / "device_summary.md"
        )
        # Billed every turn as part of the floor → keep tight (~1.5k tokens).
        self._summary_max_chars: int = app_config.REALTIME_SUMMARY_MAX_CHARS
        # Raw archive — append-only, trimmed by flushing oldest
        self._raw_memory_path: Path = self._realtime_memory_path.with_name(
            "memory_raw.jsonl"
        )
        # Lock for concurrent access to memory files
        self._realtime_memory_lock: threading.Lock = threading.Lock()
        self._realtime_summarize_lock: threading.Lock = threading.Lock()

    # --- Abstract methods (subclasses implement) ---

    @abstractmethod
    def load_device_context(self) -> str:
        """Load agent identity context (e.g. SOUL.md, IDENTITY.md, USER.md)."""

    @abstractmethod
    def load_device_memory(self) -> list[str]:
        """Load device memory entries (summary + unsummarized recent files)."""

    @abstractmethod
    def load_skills_catalog(self) -> str:
        """Load skill definitions as a formatted string (e.g. markdown table)."""

    @abstractmethod
    def summarize_device_memory(self) -> None:
        """Summarize device memory files into a persistent summary."""

    # --- Public API (shared logic) ---

    def summarize_realtime_memory(self) -> None:
        """Summarize entries in memory.jsonl into summary.md, keeping entries added during summarization."""
        if not self._summarizer:
            return
        with self._realtime_summarize_lock:
            try:
                to_summarize: list[str] = []
                with self._realtime_memory_lock:
                    if not self._realtime_memory_path.exists():
                        return

                    raw: str = self._realtime_memory_path.read_text(
                        encoding="utf-8"
                    ).strip()

                    if not raw:
                        return

                    lines: list[str] = raw.splitlines()
                    lines_read: int = len(lines)
                    entries: list[str] = self._parse_jsonl_lines(lines)
                    if not entries:
                        return

                    if self._summary_path.exists():
                        try:
                            existing: str = self._summary_path.read_text(
                                encoding="utf-8"
                            ).strip()
                            if existing:
                                to_summarize.append(f"[Previous summary]\n{existing}")
                        except Exception:
                            pass
                    to_summarize.extend(entries)

                logger.info(
                    "[realtime] Summarizing %d realtime memory entries...", len(entries)
                )
                new_summary: str = self._summarizer.summarize(to_summarize)
                if new_summary:
                    with self._realtime_memory_lock:
                        self._summary_path.write_text(
                            new_summary + "\n", encoding="utf-8"
                        )
                        # Only remove the lines we read — keep any new entries added during summarization
                        current_lines: list[str] = (
                            self._realtime_memory_path.read_text(encoding="utf-8")
                            .strip()
                            .splitlines()
                        )
                        remaining: list[str] = current_lines[lines_read:]
                        self._realtime_memory_path.write_text(
                            "\n".join(remaining) + "\n" if remaining else "",
                            encoding="utf-8",
                        )
                    logger.info(
                        "[realtime] Realtime memory summarization complete → summary.md (kept %d new entries)",
                        len(remaining),
                    )
            except Exception as e:
                logger.exception(
                    "[realtime] Failed to summarize realtime memory due to %s", e
                )

    def build_instructions(self) -> str:
        """Build the full instruction string from all context sources.

        This whole block is the per-turn "floor": it is set once as the model's
        system_instruction, but the provider re-bills it as input context on EVERY
        turn. The breakdown logged here (chars + ~token estimate, ~4 chars/token)
        shows which section dominates the floor so cost cuts can be targeted.
        """
        sections: list[str] = []
        sizes: list[tuple[str, int]] = []

        def add(label: str, text: str) -> None:
            if text:
                sections.append(text)
                sizes.append((label, len(text)))

        add("prompt", self._load_system_prompt())

        identity: str = self.load_device_context()
        add("identity", f"# DEVICE IDENTITY\n\n{identity}" if identity else "")

        catalog: str = self.load_skills_catalog()
        add("skills", f"# SKILLS CATALOG\n\n{catalog}" if catalog else "")

        device_mem: list[str] = self.load_device_memory()
        add("device_mem", "# DEVICE MEMORY\n\n" + "\n\n".join(device_mem) if device_mem else "")

        rt_mem: list[str] = self.load_realtime_memory()
        add("realtime_mem", "# REALTIME MEMORY\n\n" + "\n\n".join(rt_mem) if rt_mem else "")

        result: str = "\n\n".join(sections)
        total: int = len(result)
        breakdown: str = "  ".join(f"{label}={c}c(~{c // 4}t)" for label, c in sizes)
        logger.info(
            "[realtime] floor breakdown: total=%dc (~%dt billed EVERY turn) | %s",
            total,
            total // 4,
            breakdown,
        )
        return result

    def add_turn(self, user_text: str, agent_text: str) -> None:
        """Save a conversation turn to both working memory and raw archive."""
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "user": user_text,
            "agent": agent_text,
        }
        line: str = json.dumps(entry, ensure_ascii=False) + "\n"
        try:
            with self._realtime_memory_lock:
                self._realtime_memory_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._realtime_memory_path, "a", encoding="utf-8") as f:
                    f.write(line)
                with open(self._raw_memory_path, "a", encoding="utf-8") as f:
                    f.write(line)
            self._trim_memory_if_needed()
        except Exception as e:
            logger.warning("[realtime] Failed to save realtime memory: %s", e)

    # --- Private shared helpers ---

    @staticmethod
    def _format_jsonl_entry(line: str) -> str:
        """Parse a JSONL line into a formatted string. Returns empty string on failure."""
        try:
            entry: dict[str, Any] = json.loads(line)
            ts: str = entry.get("ts", "")
            user: str = entry.get("user", "")
            agent: str = entry.get("agent", "")
            return f"[{ts}] User: {user} | Agent: {agent}"
        except (json.JSONDecodeError, KeyError):
            return ""

    @staticmethod
    def _parse_jsonl_lines(lines: list[str]) -> list[str]:
        """Parse multiple JSONL lines into formatted strings, skipping failures."""
        entries: list[str] = []
        for line in lines:
            formatted: str = ContextManagerBase._format_jsonl_entry(line)
            if formatted:
                entries.append(formatted)
        return entries

    def _load_system_prompt(self) -> str:
        """Load provider-specific system prompt with {language} placeholder resolved."""
        prompt_path: Path = self.PROVIDER_PROMPT_PATHS.get(
            self._provider, self.DEFAULT_PROMPT_PATH
        )
        if not prompt_path.exists():
            prompt_path = self.DEFAULT_PROMPT_PATH
        try:
            template: str = prompt_path.read_text(encoding="utf-8").strip()
            lang_name: str = self.LANGUAGE_NAMES.get(self._language, self._language)
            return template.replace("{language}", lang_name)
        except FileNotFoundError:
            return ""

    def load_realtime_memory(self) -> list[str]:
        """Load existing summary + latest entries from realtime memory JSONL."""
        with self._realtime_memory_lock:
            return self._load_realtime_memory_unlocked()

    def _load_realtime_memory_unlocked(self) -> list[str]:
        entries: list[str] = []

        if self._summary_path.exists():
            try:
                summary: str = self._summary_path.read_text(encoding="utf-8").strip()
                if summary:
                    entries.append(f"[Previous summary]\n{summary}")
            except Exception as e:
                logger.warning("[realtime] Failed to read summary: %s", e)

        if not self._realtime_memory_path.exists():
            return entries

        try:
            lines: list[str] = (
                self._realtime_memory_path.read_text(encoding="utf-8")
                .strip()
                .splitlines()
            )
        except Exception as e:
            logger.warning("[realtime] Failed to read realtime memory: %s", e)
            return entries

        total_chars: int = sum(len(e) for e in entries)
        selected_lines: list[str] = []
        for line in reversed(lines):
            formatted: str = self._format_jsonl_entry(line)
            if not formatted:
                continue
            if total_chars + len(formatted) > self._realtime_memory_max_chars:
                break
            selected_lines.append(formatted)
            total_chars += len(formatted)
        selected_lines.reverse()
        entries.extend(selected_lines)
        return entries

    def _trim_memory_if_needed(self) -> None:
        """Summarize working memory in background and trim raw archive."""
        try:
            needs_summarize: bool = False

            if self._realtime_memory_path.exists() and self._summarizer:
                with self._realtime_memory_lock:
                    raw: str = self._realtime_memory_path.read_text(
                        encoding="utf-8"
                    ).strip()
                    needs_summarize = len(raw) > self._realtime_memory_max_chars

            with self._realtime_memory_lock:
                if self._raw_memory_path.exists():
                    raw_lines: list[str] = (
                        self._raw_memory_path.read_text(encoding="utf-8")
                        .strip()
                        .splitlines()
                    )
                    if len(raw_lines) > self._max_memory_entries:
                        kept: list[str] = raw_lines[-self._trim_keep :]
                        self._raw_memory_path.write_text(
                            "\n".join(kept) + "\n", encoding="utf-8"
                        )
                        logger.info(
                            "Trimmed memory_raw.jsonl: %d → %d entries",
                            len(raw_lines),
                            len(kept),
                        )

            if needs_summarize:
                logger.info(
                    "[realtime] Memory.jsonl exceeds char limit — summarizing in background"
                )
                threading.Thread(
                    target=self.summarize_realtime_memory,
                    daemon=True,
                    name="rt-summarize",
                ).start()

        except Exception as e:
            logger.warning("[realtime] Failed to trim realtime memory: %s", e)
