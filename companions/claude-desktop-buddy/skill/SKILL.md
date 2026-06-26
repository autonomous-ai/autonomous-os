---
name: claude-buddy
description: MOVED — canonical skill now lives in the platform skills tree.
---

# Moved

The on-device agent skill that turns Claude approval prompts into a voice
interaction (and surfaces Claude activity) is now a **platform skill** so it
ships to devices through the standard skill pipeline:

- Source of truth: **`skills/claude-buddy/SKILL.md`** (repo root)
- Registered in `os/services/internal/skills/skills.go` (`Catalog` + `Capability`
  → `audio`) and `scripts/provision/setup.sh` (`skill_caps`)
- Deployed via `scripts/release/upload-skills.sh` (OTA) → loaded by the agent
  runtime (`openclaw/skill_watcher`)

This file is kept only as a pointer; do not edit skill behavior here.
