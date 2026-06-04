# Contributing to Autonomous

Autonomous is the open-source OS for physical AI agents. Lamp and Intern are reference
devices built on it — and you can build your own.

## The shape of a contribution

| You want to… | You write… |
|--------------|-----------|
| Add a new device / body | `devices/<id>/DEVICE.md` + any missing drivers |
| Add a hardware capability | a driver in `os/hal/drivers/<subsystem>/` implementing the HAL contract |
| Support a new board | a profile in `os/hal/platform/<board>/` (see `docs/board-bringup.md`) |
| Teach the agent a new ability | a `skills/<name>/SKILL.md` |
| Give a device character | a `SOUL.md` |

**You never fork the OS to add a device.** If you find yourself forking, the contract is
missing something — open an issue.

## Rules

- **Code is the source of truth; docs reflect code.** Update EN + VI docs in the same PR
  when behavior changes (see the doc-sync table in `CLAUDE.md` / `AGENTS.md`).
- **The contract is frozen.** Changes under `contract/` are ABI changes — they need
  maintainer sign-off and follow the versioning policy in `DEVICE-SPEC.md`.
- **Safety is deterministic.** Anything governed by `SAFETY.md` is enforced in policy,
  never by prompting the gateway.
- **Tests required.** Every change ships with tests; the suite is green before merge.
- Comments in English.

## License

By contributing, you agree your contributions are licensed under Apache 2.0.
