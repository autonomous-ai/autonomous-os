"""Realtime context manager — builds instructions from lamp identity, skills, and memory."""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import hal.config as app_config
from hal.drivers.realtime.constants import RESOURCES_DIR
from hal.drivers.realtime.summarizer import RealtimeSummarizer

logger = logging.getLogger(__name__)


class RealtimeContextManager:
    """Builds rich instructions for the realtime voice agent from lamp context."""

    DEFAULT_PROMPT_PATH: Path = RESOURCES_DIR / "system_prompt.md"

    # Regex to extract YAML frontmatter from SKILL.md
    FRONTMATTER_RE: re.Pattern[str] = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
    NAME_RE: re.Pattern[str] = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
    DESC_RE: re.Pattern[str] = re.compile(r"^description:\s*(.+)$", re.MULTILINE)

    def __init__(
        self,
        workspace_dir: str = app_config.REALTIME_WORKSPACE_DIR,
        realtime_memory_path: str = app_config.REALTIME_MEMORY_PATH,
        language: str | None = None,
        max_memory_entries: int = app_config.REALTIME_MAX_MEMORY_ENTRIES,
        trim_keep: int = app_config.REALTIME_MEMORY_TRIM_KEEP,
        lamp_memory_max_chars: int = app_config.REALTIME_DEVICE_MEMORY_MAX_CHARS,
        realtime_memory_max_chars: int = app_config.REALTIME_MEMORY_MAX_CHARS,
        summarizer: RealtimeSummarizer | None = None,
    ) -> None:
        self._workspace: Path = Path(workspace_dir)
        self._realtime_memory_path: Path = Path(realtime_memory_path)
        self._language: str = language or "English"
        self._max_memory_entries: int = max_memory_entries
        self._trim_keep: int = trim_keep
        self._lamp_memory_max_chars: int = lamp_memory_max_chars
        self._realtime_memory_max_chars: int = realtime_memory_max_chars
        self._summarizer: RealtimeSummarizer | None = summarizer
        # Summary file alongside the memory JSONL
        self._summary_path: Path = self._realtime_memory_path.parent / "summary.md"

    # --- Public API ---

    def build_instructions(self) -> str:
        """Build the full instruction string from all context sources.

        If a summarizer is set, lamp memory and realtime memory are
        summarized via LLM before injection.
        """
        sections: list[str] = []

        # System prompt
        prompt: str = self._load_system_prompt()
        if prompt:
            sections.append(prompt)

        # Lamp identity
        identity: str = self._load_lamp_identity()
        if identity:
            sections.append(f"# LAMP IDENTITY\n\n{identity}")

        # Skills catalog
        catalog: str = self._load_skills_catalog()
        if catalog:
            sections.append(f"# SKILLS CATALOG\n\n{catalog}")

        # Lamp memory
        lamp_mem_raw: list[str] = self._load_lamp_memory_entries()
        if lamp_mem_raw:
            lamp_mem: str = self._summarize_or_join(lamp_mem_raw)
            if lamp_mem:
                sections.append(f"# LAMP MEMORY\n\n{lamp_mem}")

        # Realtime memory
        rt_mem_raw: list[str] = self._load_realtime_memory_entries()
        if rt_mem_raw:
            rt_mem: str = self._summarize_or_join(rt_mem_raw)
            if rt_mem:
                sections.append(f"# REALTIME MEMORY\n\n{rt_mem}")

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
        """Save a conversation turn to the realtime memory file."""
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "user": user_text,
            "agent": agent_text,
        }
        try:
            self._realtime_memory_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._realtime_memory_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._trim_memory_if_needed()
        except Exception as e:
            logger.warning("Failed to save realtime memory: %s", e)

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
        """Load system_prompt.md with {language} placeholder resolved to full name."""
        try:
            template: str = self.DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").strip()
            lang_name: str = self.LANGUAGE_NAMES.get(self._language, self._language)
            return template.replace("{language}", lang_name)
        except FileNotFoundError:
            return ""

    def _load_lamp_identity(self) -> str:
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
                logger.warning("Failed to read %s: %s", path, e)
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
                logger.warning("Failed to parse %s: %s", skill_md, e)

        if not rows:
            return ""

        lines: list[str] = ["| Skill | Description |", "|-------|-------------|"]
        for name, desc in rows:
            lines.append(f"| {name} | {desc} |")
        return "\n".join(lines)

    def _summarize_or_join(self, entries: list[str]) -> str:
        """Summarize entries via LLM if summarizer is available, otherwise join raw."""
        if self._summarizer and entries:
            summary: str = self._summarizer.summarize(entries)
            if summary:
                return summary
        return "\n\n".join(entries)

    def _load_lamp_memory_entries(self) -> list[str]:
        """Load entries from workspace/memory/*.md up to char budget."""
        memory_dir: Path = self._workspace / "memory"
        if not memory_dir.is_dir():
            return []

        md_files: list[Path] = sorted(memory_dir.glob("*.md"), reverse=True)

        entries: list[str] = []
        total_chars: int = 0
        for md_file in md_files:
            try:
                content: str = md_file.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                entry: str = f"## {md_file.stem}\n\n{content}"
                if total_chars + len(entry) > self._lamp_memory_max_chars:
                    break
                entries.append(entry)
                total_chars += len(entry)
            except Exception as e:
                logger.warning("Failed to read memory %s: %s", md_file, e)
        return entries

    def _load_realtime_memory_entries(self) -> list[str]:
        """Load existing summary + latest N entries from realtime memory JSONL."""
        entries: list[str] = []

        # Load existing summary if present
        if self._summary_path.exists():
            try:
                summary: str = self._summary_path.read_text(encoding="utf-8").strip()
                if summary:
                    entries.append(f"[Previous summary]\n{summary}")
            except Exception as e:
                logger.warning("Failed to read summary: %s", e)

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
            logger.warning("Failed to read realtime memory: %s", e)
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
        """If realtime memory exceeds max entries, summarize old ones instead of discarding."""
        try:
            lines: list[str] = (
                self._realtime_memory_path.read_text(encoding="utf-8")
                .strip()
                .splitlines()
            )
            if len(lines) <= self._max_memory_entries:
                return

            # Split into old (to summarize) and recent (to keep)
            old_lines: list[str] = lines[: -self._trim_keep]
            kept: list[str] = lines[-self._trim_keep :]

            # Summarize old entries if summarizer is available
            if self._summarizer and old_lines:
                old_entries: list[str] = self._parse_jsonl_lines(old_lines)

                # Load existing summary and include it
                existing_summary: str = ""
                if self._summary_path.exists():
                    try:
                        existing_summary = self._summary_path.read_text(encoding="utf-8").strip()
                    except Exception:
                        pass

                to_summarize: list[str] = []
                if existing_summary:
                    to_summarize.append(f"[Previous summary]\n{existing_summary}")
                to_summarize.extend(old_entries)

                new_summary: str = self._summarizer.summarize(to_summarize)
                if new_summary:
                    self._summary_path.write_text(new_summary + "\n", encoding="utf-8")
                    logger.info("Summarized %d old entries into summary.md", len(old_entries))

            # Keep only recent entries
            self._realtime_memory_path.write_text(
                "\n".join(kept) + "\n", encoding="utf-8"
            )
            logger.info(
                "Trimmed realtime memory: %d → %d entries",
                len(lines),
                len(kept),
            )
        except Exception as e:
            logger.warning("Failed to trim realtime memory: %s", e)
