"""Realtime context manager — builds instructions from device identity, skills, and memory."""

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import hal.config as app_config
from hal.drivers.realtime.constants import RESOURCES_DIR
from hal.drivers.realtime.summarizer import RealtimeSummarizer

logger = logging.getLogger(__name__)


class RealtimeContextManager:
    """Builds rich instructions for the realtime voice agent from device context."""

    DEFAULT_PROMPT_PATH: Path = RESOURCES_DIR / "system_prompt.md"
    PROVIDER_PROMPT_PATHS: dict[str, Path] = {
        "openai": RESOURCES_DIR / "system_prompt_openai.md",
        "gemini": RESOURCES_DIR / "system_prompt_gemini.md",
    }

    # Regex to extract YAML frontmatter from SKILL.md
    FRONTMATTER_RE: re.Pattern[str] = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
    NAME_RE: re.Pattern[str] = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
    DESC_RE: re.Pattern[str] = re.compile(r"^description:\s*(.+)$", re.MULTILINE)

    def __init__(
        self,
        workspace_dir: str = app_config.REALTIME_WORKSPACE_DIR,
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
        self._summary_max_chars: int = 10000
        # Raw archive — append-only, trimmed by flushing oldest
        self._raw_memory_path: Path = self._realtime_memory_path.with_name(
            "memory_raw.jsonl"
        )
        # Lock for concurrent access to memory files
        self._realtime_memory_lock: threading.Lock = threading.Lock()
        self._realtime_summarize_lock: threading.Lock = threading.Lock()

    # --- Public API ---

    def summarize_device_memory(self) -> None:
        """Summarize device memory files modified after the last device_summary.md.

        Reads files newer than device_summary.md by mtime, summarizes them
        together with the existing summary, and writes the result.
        """
        if not self._summarizer:
            return
        memory_dir: Path = self._workspace / "memory"
        if not memory_dir.is_dir():
            return

        summary_mtime: float = (
            self._device_summary_path.stat().st_mtime
            if self._device_summary_path.exists()
            else 0.0
        )

        # Collect files modified after the last summary
        new_files: list[Path] = [
            f
            for f in sorted(memory_dir.glob("*.md"), key=lambda f: f.stat().st_mtime)
            if f.stat().st_mtime > summary_mtime
        ]
        if not new_files:
            return

        new_entries: list[str] = []
        total_chars: int = 0
        for md_file in new_files:
            try:
                content: str = md_file.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                entry: str = f"## {md_file.stem}\n\n{content}"
                remaining = self._device_memory_max_chars - total_chars
                if remaining <= 0:
                    break
                new_entries.append(f"...{entry[-remaining:]}")
                total_chars += len(new_entries[-1])
            except Exception as e:
                logger.warning("[realtime] Failed to read memory %s: %s", md_file, e)
        if not new_entries:
            return

        to_summarize: list[str] = []
        if self._device_summary_path.exists():
            try:
                existing: str = self._device_summary_path.read_text(
                    encoding="utf-8"
                ).strip()
                if existing:
                    to_summarize.append(f"[Previous summary]\n{existing}")
            except Exception:
                pass
        to_summarize.extend(new_entries)

        logger.info(
            "[realtime] Summarizing %d new device memory files...", len(new_files)
        )
        new_summary: str = self._summarizer.summarize(to_summarize)
        if new_summary:
            self._device_summary_path.parent.mkdir(parents=True, exist_ok=True)
            self._device_summary_path.write_text(new_summary + "\n", encoding="utf-8")
            logger.info(
                "[realtime] Device memory summarization complete → device_summary.md"
            )

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
        """Build the full instruction string from all context sources."""
        sections: list[str] = []

        # System prompt
        prompt: str = self._load_system_prompt()
        if prompt:
            sections.append(prompt)

        # Device identity
        identity: str = self._load_device_identity()
        if identity:
            sections.append(f"# DEVICE IDENTITY\n\n{identity}")

        # Skills catalog
        catalog: str = self._load_skills_catalog()
        if catalog:
            sections.append(f"# SKILLS CATALOG\n\n{catalog}")

        # Device memory — device_summary.md + unsummarized recent files
        device_mem_raw: list[str] = self._load_device_memory_entries()
        if device_mem_raw:
            sections.append("# DEVICE MEMORY\n\n" + "\n\n".join(device_mem_raw))

        # Realtime memory — summary.md + unsummarized entries from memory.jsonl
        rt_mem_raw: list[str] = self._load_realtime_memory_entries()
        if rt_mem_raw:
            sections.append("# REALTIME MEMORY\n\n" + "\n\n".join(rt_mem_raw))

        return "\n\n".join(sections)

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
            formatted: str = RealtimeContextManager._format_jsonl_entry(line)
            if formatted:
                entries.append(formatted)
        return entries

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
                # Working memory (summarized periodically, then cleared)
                with open(self._realtime_memory_path, "a", encoding="utf-8") as f:
                    f.write(line)
                # Raw archive (append-only, trimmed by flushing oldest)
                with open(self._raw_memory_path, "a", encoding="utf-8") as f:
                    f.write(line)
            self._trim_memory_if_needed()
        except Exception as e:
            logger.warning("[realtime] Failed to save realtime memory: %s", e)

    # --- Private loaders ---

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

    def _load_system_prompt(self) -> str:
        """Load provider-specific system prompt with {language} placeholder resolved.

        Falls back to the shared system_prompt.md if no provider-specific file exists.
        """
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

    def _load_device_identity(self) -> str:
        """Load SOUL.md, IDENTITY.md, and USER.md from the workspace."""
        parts: list[str] = []
        for filename in ("SOUL.md", "IDENTITY.md", "USER.md"):
            path: Path = self._workspace / filename
            try:
                content: str = path.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(content)
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning("[realtime] Failed to read %s: %s", path, e)
        return "\n\n".join(parts)

    def _load_skills_catalog(self) -> str:
        """Parse SKILL.md frontmatter from all skills, return a markdown table."""
        skills_dir: Path = self._workspace / "skills"
        if not skills_dir.is_dir():
            return ""

        rows: list[tuple[str, str]] = []
        for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
            try:
                text: str = skill_md.read_text(encoding="utf-8")
                fm_match = self.FRONTMATTER_RE.match(text)
                if not fm_match:
                    continue
                frontmatter: str = fm_match.group(1)
                name_match = self.NAME_RE.search(frontmatter)
                desc_match = self.DESC_RE.search(frontmatter)
                name: str = (
                    name_match.group(1).strip() if name_match else skill_md.parent.name
                )
                desc: str = desc_match.group(1).strip() if desc_match else ""
                if name:
                    rows.append((name, desc))
            except Exception as e:
                logger.warning("[realtime] Failed to parse %s: %s", skill_md, e)

        if not rows:
            return ""

        lines: list[str] = ["| Skill | Description |", "|-------|-------------|"]
        for name, desc in rows:
            lines.append(f"| {name} | {desc} |")
        return "\n".join(lines)

    def _load_device_memory_entries(self) -> list[str]:
        """Load device_summary.md + unsummarized memory files (modified after last summary)."""
        entries: list[str] = []

        # Load existing device summary
        if self._device_summary_path.exists():
            try:
                summary: str = self._device_summary_path.read_text(
                    encoding="utf-8"
                ).strip()
                if summary:
                    entries.append(f"[Previous summary]\n{summary}")
            except Exception as e:
                logger.warning("[realtime] Failed to read device summary: %s", e)

        # Load memory files modified after the device summary
        memory_dir: Path = self._workspace / "memory"
        if not memory_dir.is_dir():
            return entries

        summary_mtime: float = (
            self._device_summary_path.stat().st_mtime
            if self._device_summary_path.exists()
            else 0.0
        )

        md_files: list[Path] = sorted(
            memory_dir.glob("*.md"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        total_chars: int = sum(len(e) for e in entries)
        for md_file in md_files:
            if md_file.stat().st_mtime <= summary_mtime:
                break  # Older than summary — already summarized
            try:
                content: str = md_file.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                entry: str = f"## {md_file.stem}\n\n{content}"
                if total_chars + len(entry) > self._device_memory_max_chars:
                    break
                entries.append(entry)
                total_chars += len(entry)
            except Exception as e:
                logger.warning("[realtime] Failed to read memory %s: %s", md_file, e)
        return entries

    def _load_realtime_memory_entries(self) -> list[str]:
        """Load existing summary + latest entries from realtime memory JSONL."""
        with self._realtime_memory_lock:
            return self._load_realtime_memory_entries_unlocked()

    def _load_realtime_memory_entries_unlocked(self) -> list[str]:
        entries: list[str] = []

        # Load existing summary if present
        if self._summary_path.exists():
            try:
                summary: str = self._summary_path.read_text(encoding="utf-8").strip()
                if summary:
                    entries.append(f"[Previous summary]\n{summary}")
            except Exception as e:
                logger.warning("[realtime] Failed to read summary: %s", e)

        # Load recent JSONL entries
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

        # Load entries from the end until char budget is reached
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

            # Check if working memory exceeds char limit
            if self._realtime_memory_path.exists() and self._summarizer:
                with self._realtime_memory_lock:
                    raw: str = self._realtime_memory_path.read_text(
                        encoding="utf-8"
                    ).strip()
                    needs_summarize = len(raw) > self._realtime_memory_max_chars

            # Raw archive: flush oldest half
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

            # Background summarization
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
