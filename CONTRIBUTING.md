# Contributing

Autonomous is an open-source OS for physical AI agents — and we'd love your help. PRs welcome,
vibe-coded ones too. 🤖

## What you can build

| You want to… | You write… |
|--------------|-----------|
| Run Autonomous on a new device (any hardware, any vendor) | `devices/<id>/DEVICE.md` + any missing driver |
| Add a hardware capability | a driver in `os/hal/drivers/<subsystem>/` |
| Support a new board | a profile in `os/hal/board/` |
| Teach the agent a new ability | a `skills/<name>/SKILL.md` |
| Give a device a personality | a `SOUL.md` |

You never fork the OS to add a device. If you're forking, the contract is missing something —
open an issue and let's fix it.

## A few norms (not rules)

- Keep PRs focused; green CI helps us merge faster.
- `contract/` is the stable interface everyone builds on — open an issue before changing it.
- Be kind.

Questions? [Open an issue](https://github.com/autonomous-ai/autonomous/issues).
