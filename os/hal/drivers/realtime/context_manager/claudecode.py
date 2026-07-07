"""Claude Code context manager — OpenClaw workspace layout, skills under .claude/skills."""

from hal.drivers.realtime.context_manager.openclaw import OpenClawContextManager


class ClaudeCodeContextManager(OpenClawContextManager):
    """Context manager for the Claude Code agent runtime.

    The workspace (/root/.claudecode/workspace) copies OpenClaw's layout for
    identity and memory (SOUL.md / IDENTITY.md / USER.md / memory/*.md), but
    skills are native Claude Code skills in .claude/skills/<name>/ — the
    claude CLI auto-loads that directory, so the os-server skill watcher
    (internal/claudecode/skill_watcher.go) installs them there instead of
    workspace/skills.
    """

    SKILLS_SUBDIR: tuple[str, ...] = (".claude", "skills")
