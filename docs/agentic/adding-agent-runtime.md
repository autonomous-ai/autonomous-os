# Adding an Agentic Runtime Backend

How to add a new agent "brain" (like OpenClaw, Hermes, PicoClaw) to the OS, and
**everything it must wire up** so it reaches parity. Written from the lessons of
the Hermes port, where too many pieces were left as silent no-ops and bit us
later (config that didn't survive a factory reset, skills that never came back,
identity rename that did nothing, no skill auto-update).

> **The one rule to remember:** the OS (`os-server`) is the platform; the backend
> is a swappable brain. Anything user-visible that OpenClaw does, a new backend
> must either do too or **consciously decide to skip — and say why in a comment.**
> A no-op is a decision, never a default.

Source of truth for the contract: `os/services/domain/agent.go` (the
`AgentGateway` interface). This doc explains *which* parts matter and *how* to
wire the switch, install, migration, skills, hooks, and reset.

> **Agentic-backend docs:** this file (generic contract + how to add one) ·
> [`hermes.md`](hermes.md) (Hermes, a full backend) · [`picoclaw.md`](picoclaw.md)
> (PicoClaw, currently client-only). Per-backend protocol/quirks live in those;
> the generic mechanics + checklist live here.

---

## 0. Mental model

- `config.agent_runtime` (`/root/config/config.json`) selects the active backend.
- `internal/agent/factory.go` `ProvideGateway` resolves it at boot via Wire DI:
  `config.agent_runtime` > DEVICE.md `gateway.default` > openclaw.
- Switching at runtime goes through one core — `device.Service.UpdateAgentRuntime`
  — fired by 3 triggers (MQTT `agent_runtime.set`, HTTP `/api/device/agent-runtime`,
  web Runtime section). See `docs/agentic/hermes.md` §10–§11.

---

## 1. The contract — implement `domain.AgentGateway`

Your backend lives in `internal/<name>/` and its `*Service` must satisfy the
**whole** `AgentGateway` interface. The methods fall into groups:

| Group | Examples | New-backend stance |
|-------|----------|--------------------|
| **Core turn** | `SendChatMessage`, `SendSystemChatMessage`, `*WithImage`, `NextChatRunID`, `*WithRun`, `StartWS` | **MUST** — this is the agent. |
| **Readiness / busy** | `IsReady`, `ConnectedAt`, `AgentUptime`, `IsBusy`, `SetBusy`, `QueuePendingEvent` | **MUST** — os-server gates sensing on these. |
| **Identity** | `Name`, `Version`, `GetSessionKey`/`SetSessionKey` | **MUST** — surfaces in web Status. |
| **HAL passthrough** | `SendToHALTTS*`, `StopTTS`, `SetVolume`, `StartHALVoice` | Usually identical across backends — share or copy. |
| **Run markers** | `MarkGuardRun`/`Consume*`, `MarkBroadcastRun`, `MarkPoseBucketRun`, `MarkWebChatRun`, `MarkSilentRun`, `*PendingChatTrace*` | **MUST track** — os-server tags turns by runID; these are runtime-shaped but the OS depends on them. |
| **Channels** | `AddChannel`, `RefreshChannelConfig`, `PairWhatsapp`, `HasWhatsappSession`, `GetTelegram*`, `Broadcast`, `SendToUser*` | No-op only if the backend genuinely can't (e.g. WhatsApp needs a Baileys plugin). Telegram usually still works via Lumi config. |
| **Lifecycle / onboarding** | `SetupAgent`, `EnsureOnboarding`, `ResetAgent`, `RestartAgent`, `RefreshModelsConfig` | Decide per backend; document the no-op. |
| **Migration-adjacent** | `UpdateIdentityName`, `StartSkillWatcher`, `WatchIdentity`, `StartModelSync`, `UpdatePrimaryModel`, `StartPrimaryModelWatch`, `CompactSession`, `NewSession`, `FetchChatHistory`, `WriteMCPEntry`/`RemoveMCPEntry`, `GetConfigJSON` | **The danger zone** — easy to no-op, expensive to discover missing. See §4–§6. |

**Lesson (Hermes):** ~15 of these were stubbed no-op in `internal/hermes/stubs.go`.
Some are legitimately N/A (`WriteMCPEntry` — no `openclaw.json`; `PairWhatsapp` —
no plugin). But `StartSkillWatcher`, `UpdateIdentityName`, and the config-sync path
were **functional gaps**, not N/A — they shipped as no-ops and we only noticed when
skills went stale / rename did nothing / config broke after a reset. Audit every
stub: write `// no-op because <reason>` or `// TODO(<backend>-<feature>)`, never a
bare empty body.

---

## 2. Register + wire the switch

1. `domain/device.go`: add `AgentRuntime<Name>` const + the entry in `AgentRuntimes`.
2. `internal/agent/factory.go`: add the `case` in `ProvideGateway`.
3. **Embedded installer**: `internal/<name>/install.sh` + `install.go`
   (`//go:embed install.sh` → `runtimereg.Register(name, InstallScript)`).
4. `switch_runtime.sh` is **generic** — it knows no backend names. Do **not** edit
   it, the imager, or os-server's switch core to add a backend.

The installer contract (`switch_runtime.sh` expects):
- create a systemd unit; declare its name in `/usr/local/lib/os-runtimes/<name>/service`
  if it isn't `<name>.service`.
- optionally drop a `verify` hook at `/usr/local/lib/os-runtimes/<name>/verify`
  (exit 0 = "installed & usable"). Keep it **cheap** — see §3 for why it must not
  over-check.

### The switch flow (what happens on a switch)

`device.Service.UpdateAgentRuntime` validates the runtime, captures the active
`old`, then runs the switcher under `systemd-run --wait` and **blocks on its exit
code**. `config.agent_runtime` is persisted **only after a clean exit 0** — so a
crash/reboot mid-switch resolves the still-installed `old`, and there is nothing to
revert. `switch-runtime <new> <old>` (generic, `internal/device/switch_runtime.sh`,
`go:embed`-materialized to `/usr/local/bin/switch-runtime`):

1. resolves `<new>`'s unit name (default `<new>.service`, or the name declared in
   `/usr/local/lib/os-runtimes/<new>/service`) and checks **installed AND usable** —
   unit present **and** the `verify` hook passes (no verify hook → unit-presence
   alone). If not, runs the installer (embedded copy first, CDN fallback). This
   closes the orphaned-unit trap (a stale `.service` whose binary is gone).
2. runs `/usr/local/bin/runtime-<new>-presync` (materialized by os-server — §3).
3. `enable --now <new-unit>` **and asserts it actually reached active** (a unit can
   enable cleanly yet crash on a missing binary). If it didn't start and the
   installer hadn't run this pass, it **reinstalls once and retries**. Then stops
   the old unit (up to 3 `disable --now` retries).
4. exits 0 — it does **not** restart os-server (os-server, blocked on `--wait`, acks
   the real outcome then restarts itself so `factory.go` re-resolves the gateway).
   On failure a rollback trap restarts only the **old** unit. It never touches
   `config.json`.

So `switch-runtime` is fully backend-agnostic — **no imager / setup.sh / switcher
change is ever needed to add a backend.**

---

## 3. The golden rule: install-once vs every-switch (the *activation gap*)

`install.sh` runs **once** — `switch_runtime.sh` only runs it on a first install or
when `verify` fails. On every later switch it is **skipped**.

> **Therefore: anything that must survive a factory reset, or must refresh on a
> plain `os-server` OTA, MUST NOT be written only by `install.sh`. It must be
> materialized by `os-server` on every switch.**

This is the **activation gap** and we hit it twice on Hermes:
- A fix shipped inside `install.sh` (or any file `install.sh` writes — the
  `verify` hook, the presync hook) **never reaches an already-installed device**
  via OTA: the old on-disk copy keeps passing `verify`, so `install.sh` never
  re-runs, so the new copy never lands.

The fix pattern (use it for everything stateful):
- Put the logic in a **presync hook** (`runtime-<name>-presync`).
- Embed it: `//go:embed presync.sh` → `runtimereg.RegisterPresync(name, PresyncScript)`.
- `os-server` materializes it every switch (`internal/device/runtime_installers.go`
  `materializePresync`, called from the switch flow next to `materializeInstaller`).
- `switch_runtime.sh` runs `runtime-<name>-presync` right before the backend starts.

Hermes's presync (`internal/hermes/presync.sh`) now owns **both** the
`config.yaml` model wiring (idempotent — coerces a reset-blanked `model: ''` back
to a map, asserts `provider`/`custom_providers` structure, syncs `llm_*`/secrets)
**and** the skill restore (re-runs `claw migrate` when `skills/openclaw-imports`
is empty). Keep `verify` CLI-only (`command -v <bin>`) — a structure-check in
`verify` would force a heavy full reinstall when presync alone heals it.

---

## 4. Persona + memory migration (Go, runs every switch)

**Hub-and-spoke, not per-pair.** Migration goes through a runtime-neutral
`PersonaBundle`: each runtime has ONE **read** adapter (its on-disk layout →
bundle) and ONE **write** adapter (bundle → its layout), in
`internal/agent/migrate_persona/runtime_<name>.go`. A migration is
`read[from] → write[to]` (`RunMigration(from, to, opts)`). So adding a runtime is
**one adapter file** that interoperates with every existing runtime in both
directions — file count is **linear (2 per runtime)**, not the quadratic N×(N-1)
a per-pair migrator needs. Register the adapter in the `adapters` map in
`migrator.go`; nothing else changes (no new `Direction` enum). A runtime with no
registered adapter (external/out-of-band persona, e.g. PicoClaw) is simply
skipped by `CanMigrate` — switches to/from it don't migrate.

> **Copy-me template:** `internal/agent/migrate_persona/runtime_example.go` is a
> build-ignored, fully-annotated skeleton — copy it to `runtime_<name>.go`, delete
> the `//go:build ignore` line, and fill in the 5 wiring steps + read/write. It
> spells out the per-field decision (separate slot vs inline vs fold) inline.

Migration runs at os-server boot after a real switch (`Reconcile`, when
`agent_state.json` prev ≠ current). What each adapter carries:

- **SOUL.md** → backend's identity file. If the backend has **no separate
  IDENTITY.md slot** (Hermes doesn't), inline the owner's filled IDENTITY fields
  as a `## Your identity card` block in SOUL (see `buildIdentityBlock`).
- **MEMORY.md + `memory/*.md` daily + KNOWLEDGE.md** → merged into the backend's
  long-term memory file. **First check which files the backend LOADS BY NAME** —
  Hermes loads only `MEMORY.md` + `USER.md` (no `memories/*.md` glob), so a
  separate `KNOWLEDGE.md` would be ignored; we fold it into `MEMORY.md` instead.
- **USER.md** → backend's user-profile file.
- Set **`Overwrite = true`** for the soul copy on a switch: a switch means "adopt
  the persona I was just using." `copyPersona` backs up first (`.bak-<nano>`).
- The reverse direction must **strip backend-only artifacts** it added (e.g. the
  identity card — OpenClaw keeps the name in its own IDENTITY.md) **and restore
  what the forward inlined back into its native slot**. Inlining the name into the
  Hermes SOUL but only stripping it on the way back **loses the name** — the reverse
  must parse the card and write the fields into the OpenClaw `IDENTITY.md`
  (`restoreIdentityCard`, the inverse of `ensureIdentityInlined`: line
  replace-or-append, preserving the existing template; relies on the same
  "onboard does not clobber an existing IDENTITY.md" guarantee `UpdateIdentityName`
  uses). Every forward inline needs a matching reverse restore, or round-tripping
  silently drops state.

Do **NOT** carry runtime-specific files: `AGENTS.md`, `TOOLS.md`, `HEARTBEAT.md`,
`hooks/` — they belong to the source runtime. The backend's **deep memory engine**
(episodic/semantic DB, dream-diary, grounded-short-term) is **not portable** —
the distilled `MEMORY.md`/`USER.md` is the portable form.

### Classify every migration step: fold vs move

Whether a switch round-trips losslessly is **per-backend**, decided by which slots
the target backend has. Don't assume symmetry — classify each forward step:

- **Move / inline** (a field goes to a *different* slot, e.g. IDENTITY name →
  inlined into SOUL): reversible, and the reverse **MUST** restore it to its native
  slot. Skipping the restore silently drops state — that was the identity-name bug.
  **Every inline needs a matching reverse restore.**
- **Fold** (two source structures collapse into *one* target, because the target
  backend lacks a slot for one of them): inherently lossy on **structure** (not on
  data), and has **no faithful inverse** — once merged, the entries can't be split
  back apart. Only do a fold when the target genuinely lacks the slot, and document
  it as a known one-way asymmetry **in that backend's own doc** (it is a property of
  that backend, not of the migration framework — another backend that *has* the slot
  would map 1:1 and round-trip cleanly).

So a backend's migration symmetry is a fact about *that backend's slots*, recorded
in its backend doc (e.g. `docs/agentic/hermes.md`), not a blanket guarantee here.

---

## 5. Skills

- Skills reach the backend by being **copied** (verify: copy vs convert! Hermes's
  `claw migrate` is `shutil.copytree`, no transform) into the backend's skill dir.
- **Restore-after-reset** belongs in **presync**, guarded on the dir being empty
  (so a normal switch is a no-op — no churn). See §3.
- **Skill watcher** (auto-update from CDN, capability-gated): the generic
  fetch/extract/hash plumbing is shared in `internal/skills/skillzip.go`
  (`FetchSkillVersions`/`DownloadToTempFile`/`FolderHash`/`ExtractSkillZip`). Add a
  thin `internal/<name>/skill_watcher.go` parallel to `internal/openclaw/skill_watcher.go`
  — only the **target dir** and the **notify path** differ. Gate with
  `skills.Supported(device.Capabilities(...))`. Notify the agent with
  `SendSystemChatMessage`.

---

## 6. Hooks — OS-side reimplementation for Hermes (a worked example)

OpenClaw hooks (`hooks/<name>/{HOOK.md, handler.ts}`) are TypeScript handlers that
fire on OpenClaw's `message:preprocessed` event — `emotion-acknowledge` (instant
"thinking" face on message arrival) and `turn-gate` (set busy for channel turns).
They are **runtime-specific** and not portable. A new backend does **not** inherit
them.

**Why OpenClaw needs hooks but Hermes does not.** In OpenClaw every turn runs
inside the daemon; os-server pushes the message over WebSocket and loses the
thread, so the only place to inject "thinking" at the right moment is the hook.
With Hermes the interception point is already on the Go side — **every turn sent
to Hermes flows through `internal/hermes/chat.go:sendChat`** (voice, sensing, web,
and Telegram, whose receive loop is Lumi-side — see `telegramRunOrigin`). So we
reimplement the hook natively in Go and fire from `sendChat`, instead of
materializing hook files into a workspace (Hermes has no Go onboarding to do that,
and no `~/.hermes/hooks/HOOK.yaml` loader to execute them — the `handler.ts`
copied in by `claw migrate` is dead weight under Hermes).

What was done:
- **`emotion-acknowledge` → native Go** in `internal/hermes/emotion_ack.go`
  (`fireAckEmotion`, called from `sendChat`). It mirrors `handler.ts` 1:1: same
  emotion (`thinking`, intensity `0.7`), same skip prefixes
  (`[sensing:`/`[activity]`/`[emotion]`/`[speech_emotion]` + empty), and the
  capability gate goes through the **shared registry** `skills.SupportedHooks`
  (→ `HookCapability["emotion-acknowledge"] = expression`), resolved once at
  construction (`Service.ackHookEnabled`). The TS hook's `[HANDLED]` text skip
  maps to the Go-native `IsSilentRun(runID)` check — os-server signals
  realtime-handled turns via `MarkSilentRun`, not a body marker.
- **`turn-gate` → not mirrored (redundant).** `sendChat` already marks the turn
  busy (`busySince`/`activeTurn`) before the network round-trip, so a separate
  gate would duplicate it.

> ⚠️ **Maintenance coupling — no compile-time link.** `hooks/emotion-acknowledge/
> handler.ts` (OpenClaw) and `internal/hermes/emotion_ack.go` (Hermes) are two
> independent implementations of the same behavior. **When you change one, change
> the other** — skip rules, emotion name/intensity, and capability gate must stay
> identical, or the two backends drift apart silently. Keep the cross-reference
> comments in both files.

**When a backend-native (Python) hook *would* be needed.** Only for turns that
originate **inside** the backend and never pass through `sendChat` — e.g. a
backend heartbeat or a channel the backend listens on directly (not proxied by
Lumi). The lamp has none of these today, so the Python-plugin path
(`pre_gateway_dispatch` in `~/.hermes/plugins/`, discovered by Hermes; there is
**no** `~/.hermes/hooks/HOOK.yaml` loader in the shipped build — verify the actual
loader before assuming) is **deferred as YAGNI**, not a parity gap. Build it only
when such a turn source appears.

---

## 7. Factory reset

- Implement the backend wipe **in `ResetAgent()`** (`internal/<name>/reset.go`).
  `server/system/factoryreset.go` resolves the **active gateway** and calls
  `gw.ResetAgent()` — there is **no per-backend `switch`** to keep in sync (adding
  a backend = implement `ResetAgent`, nothing in `server/system`). A backend whose
  state is owned externally (PicoClaw) ships a **no-op `ResetAgent`**, so it is
  correctly left untouched (the old `switch` defaulted PicoClaw to the OpenClaw
  wipe — a latent bug this removed). The one shared primitive, `osreset.WipePath`,
  lives in `lib/osreset` so a backend can use it without importing `server/system`.
- **Wipe `/root/config/agent_state.json` in lockstep with `config.json`** — they
  are a pair (current runtime + switch history). Leaving `agent_state.json` while
  `config.json` resets makes a stale `prev` diverge from the reset `current` and
  triggers a **spurious persona migration** that propagates wiped/stub state.
- Keep what must survive (`bootstrap.json` = OTA state).
- A wipe path that removes migrated content (skills, config) must have a
  **restore path that runs after the reset** (presync, §3/§5) — or the content is
  gone for good once `install.sh` stops re-running.

---

## 8. Capability gating

Use the runtime-agnostic platform metadata in `internal/skills`:
- `skills.Supported(deviceCaps)` for skills, `skills.SupportedHooks(deviceCaps)`
  for hooks, where `deviceCaps = device.Capabilities(config.DeviceTypeOrDefault())`.
- Never hardcode a skill/hook list per backend — gate the same way OpenClaw does.

---

## Checklist for a new backend

- [ ] `internal/<name>/` package; `*Service` implements **all** of `AgentGateway`.
- [ ] Every stub is `// no-op because …` or `// TODO(<name>-…)` — no bare bodies.
- [ ] `domain.AgentRuntime<Name>` + `AgentRuntimes` entry; `factory.go` case.
- [ ] `install.sh` + `install.go` (`//go:embed` + `runtimereg.Register`).
- [ ] **Stateful setup → `presync.sh`** (`//go:embed` + `runtimereg.RegisterPresync`),
      materialized by os-server every switch. Nothing reset-fragile lives only in
      `install.sh`.
- [ ] `verify` hook is cheap (CLI presence), not a structure check.
- [ ] `migrate_persona/runtime_<name>.go` (copy `runtime_example.go`): ONE read +
      ONE write adapter (bundle ↔ layout), registered in the `adapters` map. `read` surfaces SOUL, identity
      fields, MEMORY/USER, and any KNOWLEDGE/daily slots; `write` restores each to
      its native slot (identity → its own file, or inline if no slot) and folds
      slots the backend lacks. `Overwrite=true` for SOUL. No new `Direction` enum.
- [ ] Skills: copy-import + **restore-in-presync** (guarded) + `skill_watcher.go`
      (parallel to openclaw, shared `internal/skills/skillzip.go`).
- [ ] Hooks: backend-native or OS-side — decided & documented (not silently absent).
      If reimplemented OS-side in Go (no compile-time link to the TS hook), add
      cross-reference comments in both files so a change to one flags the other.
- [ ] `ResetAgent()` in `internal/<name>/reset.go` (factory-reset calls
      `gw.ResetAgent()` on the active gateway — no `factoryreset.go` switch); **`agent_state.json` wiped with
      `config.json`**.
- [ ] Capability gating via `skills.Supported` / `SupportedHooks`.
- [ ] Notify the agent on skill change via `SendSystemChatMessage`.
- [ ] Docs: update `docs/agentic/hermes.md`-style backend doc + this checklist if the
      contract changed.

---

## Hermes parity status (honest ledger)

**Done:** switch wiring, embedded install + presync (config self-heal, skill
restore), persona/memory migration (SOUL + inline identity, MEMORY + daily +
KNOWLEDGE, USER), `UpdateIdentityName`, skill watcher, factory-reset
`agent_state.json` lockstep, hooks (`emotion-acknowledge` reimplemented OS-side in
Go; `turn-gate` redundant — §6).

**Still open / no-op:** backend-native (Python) hooks for backend-internal turn
sources (deferred YAGNI — §6), `WriteMCPEntry`/`RemoveMCPEntry`
(`TODO(hermes-mcp)`), `CompactSession`,
`FetchChatHistory`, and the model-sync group (`StartModelSync`,
`UpdatePrimaryModel`, `StartPrimaryModelWatch`, `RefreshModelsConfig` — largely
N/A because os-server sends a fixed request model to the campaign-api custom
provider).
