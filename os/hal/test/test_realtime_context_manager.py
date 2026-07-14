"""Skill-catalog resolution for the realtime voice agent's context managers.

Regression guard: claudecode's skills are USER-scoped (/root/.claude/skills, a
symlink to the shared store), NOT under the workspace. os-server's
ensureSkillsLink deletes any project-scoped copy, so a context manager that
resolves the catalog workspace-relatively hands the voice agent an EMPTY skill
list — the device then "has" no skills over voice while chat works fine.
"""

from pathlib import Path

from hal.drivers.realtime.context_manager.claudecode import ClaudeCodeContextManager
from hal.drivers.realtime.context_manager.openclaw import OpenClawContextManager


SKILL_MD = """---
name: {name}
description: Test skill {name}.
---

# {name}
"""


def _write_skill(skills_dir: Path, name: str) -> None:
    # The catalog reads the frontmatter `name`, not the directory name — so each
    # skill must declare its OWN name, or a decoy would masquerade as the real one
    # and the assertions below could never fail.
    (skills_dir / name).mkdir(parents=True, exist_ok=True)
    (skills_dir / name / "SKILL.md").write_text(
        SKILL_MD.format(name=name), encoding="utf-8"
    )


def test_claudecode_reads_skills_from_user_scope_not_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / ".claudecode" / "workspace"
    workspace.mkdir(parents=True)

    # The project-scoped dir os-server deletes. If the manager still resolves here,
    # it would find this decoy instead of the real, user-scoped catalog.
    decoy = workspace / ".claude" / "skills"
    _write_skill(decoy, "should-not-be-found")

    user_scope = tmp_path / ".claude" / "skills"
    _write_skill(user_scope, "connectors")

    # raising=False so a regression that drops SKILLS_DIR entirely still reaches the
    # assertions below (and fails on the decoy) rather than erroring out here.
    monkeypatch.setattr(ClaudeCodeContextManager, "SKILLS_DIR", user_scope, raising=False)
    cm = ClaudeCodeContextManager(str(workspace))

    catalog = cm.load_skills_catalog()

    assert "connectors" in catalog
    assert "should-not-be-found" not in catalog


def test_openclaw_still_reads_skills_from_the_workspace(tmp_path):
    workspace = tmp_path / ".openclaw" / "workspace"
    _write_skill(workspace / "skills", "connectors")

    cm = OpenClawContextManager(str(workspace))

    assert "connectors" in cm.load_skills_catalog()
