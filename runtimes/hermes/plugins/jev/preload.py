"""Load selected skills through Hermes without executing inline shell templates."""

import json

MAX_SKILL_RESPONSE = 128 * 1024


def load_skill_context(name, task_id=None):
    from tools.skills_tool import skill_view

    # An older native API without preprocess=False must fail open, never retry
    # with preprocessing enabled: rendering a skill can execute shell snippets.
    raw = skill_view(name=name, task_id=task_id, preprocess=False)
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_SKILL_RESPONSE:
        raise ValueError("invalid skill response size")
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get("success") is not True:
        raise ValueError("native skill load rejected")
    content = result.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty skill")
    if "!`" in content:
        raise ValueError("dynamic skill requires normal loading")
    # Keep the native result intact, including absolute skill_dir, linked files,
    # setup requirements and warnings; do not silently strip readiness metadata.
    return (
        "Jev selected the following skill for this request. Its native skill_view "
        "result is already loaded below. Use these instructions to perform the task; "
        "do not call skill_view again just to read this same SKILL.md. "
        "Read linked references only when needed. Platform rules, mandatory connectors, "
        "permissions and approval requirements remain authoritative. Loading a skill "
        "does not authorize actions. If the skill is unsuitable, use normal skill discovery.\n"
        + json.dumps({"lookup_name": name, "skill": result}, ensure_ascii=False)
    )
