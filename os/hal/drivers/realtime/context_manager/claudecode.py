"""Claude Code context manager — OpenClaw workspace layout, skills at user scope."""

from pathlib import Path

from hal.drivers.realtime.context_manager.openclaw import OpenClawContextManager


class ClaudeCodeContextManager(OpenClawContextManager):
    """Context manager for the Claude Code agent runtime.

    The workspace (/root/.claudecode/workspace) copies OpenClaw's layout for
    identity and memory (SOUL.md / IDENTITY.md / USER.md / memory/*.md), but the
    skills do NOT live under it: they are USER-scoped Claude Code skills in
    /root/.claude/skills/<name>/, a symlink to the shared store
    (/root/.autonomous/skills, internal/skills.StoreDir).

    They are user-scoped because Claude Code resolves PROJECT skills relative to
    the session cwd, so a workspace-scoped install is invisible to the coding
    sessions the device spawns in other folders. os-server's ensureSkillsLink
    (internal/claudecode/onboarding.go) maintains the link and DELETES any
    project-scoped copy — so resolving the catalog against the workspace would
    find nothing and hand the realtime voice agent an empty skill list.
    """

    SKILLS_DIR = Path("/root/.claude/skills")

    def skills_dir(self) -> Path:
        return self.SKILLS_DIR
