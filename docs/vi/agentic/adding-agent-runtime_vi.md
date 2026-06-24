# Thêm một Agentic Runtime Backend

Cách thêm một "bộ não" agent mới (như OpenClaw, Hermes, PicoClaw) vào OS, và
**mọi thứ nó phải nối** để đạt parity. Viết từ bài học port Hermes — nơi quá
nhiều mảnh bị bỏ thành no-op âm thầm và cắn lại sau này (config không sống qua
factory-reset, skills mất không quay lại, đổi tên identity chẳng làm gì, skills
không tự cập nhật).

> **Một quy tắc duy nhất phải nhớ:** OS (`os-server`) là nền tảng; backend là bộ
> não thay được. Bất cứ thứ gì OpenClaw làm mà người dùng thấy được, backend mới
> phải hoặc làm theo, hoặc **quyết định bỏ một cách có ý thức — và ghi lý do
> trong comment.** No-op là một quyết định, không bao giờ là mặc định.

Nguồn chân lý cho hợp đồng: `os/services/domain/agent.go` (interface
`AgentGateway`). Doc này giải thích phần *nào* quan trọng và *cách* nối switch,
install, migration, skills, hooks, reset.

> **Nhóm docs agentic-backend:** file này (hợp đồng generic + cách thêm) ·
> [`hermes_vi.md`](hermes_vi.md) (Hermes, backend đầy đủ) ·
> [`picoclaw_vi.md`](picoclaw_vi.md) (PicoClaw, hiện chỉ-client). Protocol/quirk
> đặc thù từng backend nằm ở các file kia; cơ chế generic + checklist nằm ở đây.

---

## 0. Mô hình tư duy

- `config.agent_runtime` (`/root/config/config.json`) chọn backend đang chạy.
- `internal/agent/factory.go` `ProvideGateway` resolve lúc boot qua Wire DI:
  `config.agent_runtime` > DEVICE.md `gateway.default` > openclaw.
- Switch lúc runtime đi qua một core — `device.Service.UpdateAgentRuntime` — kích
  bởi 3 trigger (MQTT `agent_runtime.set`, HTTP `/api/device/agent-runtime`, web
  Runtime section). Xem `docs/vi/agentic/hermes_vi.md` §10–§11.

---

## 1. Hợp đồng — implement `domain.AgentGateway`

Backend nằm ở `internal/<name>/`, `*Service` của nó phải thoả **toàn bộ**
interface `AgentGateway`. Các method chia nhóm:

| Nhóm | Ví dụ | Lập trường backend mới |
|------|-------|------------------------|
| **Turn lõi** | `SendChatMessage`, `SendSystemChatMessage`, `*WithImage`, `NextChatRunID`, `*WithRun`, `StartWS` | **BẮT BUỘC** — đây là agent. |
| **Sẵn sàng / busy** | `IsReady`, `ConnectedAt`, `AgentUptime`, `IsBusy`, `SetBusy`, `QueuePendingEvent` | **BẮT BUỘC** — os-server gate sensing theo đây. |
| **Định danh** | `Name`, `Version`, `GetSessionKey`/`SetSessionKey` | **BẮT BUỘC** — hiện ở web Status. |
| **HAL passthrough** | `SendToHALTTS*`, `StopTTS`, `SetVolume`, `StartHALVoice` | Thường giống nhau giữa các backend — share hoặc copy. |
| **Run markers** | `MarkGuardRun`/`Consume*`, `MarkBroadcastRun`, `MarkPoseBucketRun`, `MarkWebChatRun`, `MarkSilentRun`, `*PendingChatTrace*` | **BẮT BUỘC track** — os-server gắn nhãn turn theo runID; OS phụ thuộc chúng. |
| **Kênh** | `AddChannel`, `RefreshChannelConfig`, `PairWhatsapp`, `HasWhatsappSession`, `GetTelegram*`, `Broadcast`, `SendToUser*` | Chỉ no-op khi backend thật sự không làm được (vd WhatsApp cần plugin Baileys). Telegram thường vẫn chạy qua config Lumi. |
| **Lifecycle / onboarding** | `SetupAgent`, `EnsureOnboarding`, `ResetAgent`, `RestartAgent`, `RefreshModelsConfig` | Quyết theo backend; ghi rõ lý do no-op. |
| **Cận-migration** | `UpdateIdentityName`, `StartSkillWatcher`, `WatchIdentity`, `StartModelSync`, `UpdatePrimaryModel`, `StartPrimaryModelWatch`, `CompactSession`, `NewSession`, `FetchChatHistory`, `WriteMCPEntry`/`RemoveMCPEntry`, `GetConfigJSON` | **Vùng nguy hiểm** — dễ no-op, đắt để phát hiện thiếu. Xem §4–§6. |

**Bài học (Hermes):** ~15 method bị stub no-op trong `internal/hermes/stubs.go`.
Một số đúng là N/A (`WriteMCPEntry` — không có `openclaw.json`; `PairWhatsapp` —
không có plugin). Nhưng `StartSkillWatcher`, `UpdateIdentityName`, và đường
config-sync là **gap chức năng**, không phải N/A — ship dạng no-op và chỉ phát
hiện khi skills bị cũ / đổi tên vô tác dụng / config gãy sau reset. Soát mọi
stub: ghi `// no-op because <lý do>` hoặc `// TODO(<backend>-<feature>)`, đừng để
thân hàm rỗng trơ.

---

## 2. Đăng ký + nối switch

1. `domain/device.go`: thêm const `AgentRuntime<Name>` + entry trong `AgentRuntimes`.
2. `internal/agent/factory.go`: thêm `case` trong `ProvideGateway`.
3. **Installer nhúng**: `internal/<name>/install.sh` + `install.go`
   (`//go:embed install.sh` → `runtimereg.Register(name, InstallScript)`).
4. `switch_runtime.sh` **generic** — không biết tên backend. **Đừng** sửa nó,
   imager, hay switch core của os-server để thêm backend.

Hợp đồng installer (`switch_runtime.sh` kỳ vọng):
- tạo systemd unit; khai tên nó ở `/usr/local/lib/os-runtimes/<name>/service` nếu
  khác `<name>.service`.
- tuỳ chọn drop `verify` hook ở `/usr/local/lib/os-runtimes/<name>/verify`
  (exit 0 = "đã cài & dùng được"). Giữ **rẻ** — xem §3 vì sao không được check quá tay.

### Luồng switch (chuyện gì xảy ra khi switch)

`device.Service.UpdateAgentRuntime` validate runtime, ghi nhận `old` đang chạy, rồi
chạy switcher dưới `systemd-run --wait` và **block chờ exit code**.
`config.agent_runtime` **chỉ được ghi sau khi exit 0 sạch** — nên crash/reboot giữa
chừng resolve về `old` vẫn đang cài, không có gì để revert. `switch-runtime <new>
<old>` (generic, `internal/device/switch_runtime.sh`, materialize qua `go:embed` ra
`/usr/local/bin/switch-runtime`):

1. phân giải tên unit của `<new>` (mặc định `<new>.service`, hoặc tên khai trong
   `/usr/local/lib/os-runtimes/<new>/service`) và kiểm tra **đã cài VÀ dùng được** —
   unit tồn tại **và** `verify` hook pass (không có verify → chỉ cần unit). Nếu chưa
   thì chạy installer (bản embed trước, CDN fallback). Gỡ bẫy unit-mồ-côi (một
   `.service` còn sót mà binary đã mất).
2. chạy `/usr/local/bin/runtime-<new>-presync` (os-server materialize — §3).
3. `enable --now <new-unit>` rồi **assert nó thực sự active** (unit có thể enable
   sạch nhưng crash ngay vì thiếu binary). Nếu không lên mà vòng này chưa cài lại,
   **cài lại một lần rồi thử lại**. Xong mới stop unit cũ (tối đa 3 lần `disable
   --now`).
4. thoát 0 — **không** restart os-server (os-server đang block ở `--wait` sẽ ack kết
   quả thật rồi tự restart để `factory.go` resolve lại gateway). Khi lỗi, trap
   rollback chỉ restart unit **cũ**. Nó không bao giờ đụng `config.json`.

Nên `switch-runtime` hoàn toàn không-biết-backend — **không cần đụng
imager/setup.sh/switcher khi thêm backend.**

---

## 3. Quy tắc vàng: install-một-lần vs mỗi-switch (*activation gap*)

`install.sh` chạy **một lần** — `switch_runtime.sh` chỉ chạy nó lúc cài đầu hoặc
khi `verify` fail. Mọi switch sau đều **bị skip**.

> **Do đó: bất cứ thứ gì phải sống qua factory-reset, hoặc phải refresh khi OTA
> `os-server` thường, KHÔNG được chỉ ghi bởi `install.sh`. Phải được os-server
> materialize MỖI switch.**

Đây là **activation gap** và Hermes dính 2 lần:
- Một fix nhét trong `install.sh` (hoặc bất kỳ file `install.sh` ghi ra — `verify`
  hook, presync hook) **không bao giờ tới được máy đã cài** qua OTA: bản cũ trên
  disk vẫn pass `verify`, nên `install.sh` không re-run, nên bản mới không bao giờ
  đáp.

Mẫu fix (dùng cho mọi thứ có state):
- Đặt logic vào **presync hook** (`runtime-<name>-presync`).
- Nhúng: `//go:embed presync.sh` → `runtimereg.RegisterPresync(name, PresyncScript)`.
- os-server materialize nó mỗi switch (`internal/device/runtime_installers.go`
  `materializePresync`, gọi trong switch flow cạnh `materializeInstaller`).
- `switch_runtime.sh` chạy `runtime-<name>-presync` ngay trước khi backend start.

Presync của Hermes (`internal/hermes/presync.sh`) giờ làm chủ **cả** model wiring
trong `config.yaml` (idempotent — coerce `model: ''` bị reset về map, khẳng định
structure `provider`/`custom_providers`, sync `llm_*`/secrets) **lẫn** restore
skills (chạy lại `claw migrate` khi `skills/openclaw-imports` rỗng). Giữ `verify`
chỉ-CLI (`command -v <bin>`) — structure-check trong `verify` sẽ ép full reinstall
nặng trong khi presync tự lành đủ rồi.

---

## 4. Migrate persona + memory (Go, chạy mỗi switch)

**Hub-and-spoke, không phải per-pair.** Migration đi qua một `PersonaBundle`
trung lập: mỗi runtime có MỘT adapter **read** (layout đĩa → bundle) và MỘT
adapter **write** (bundle → layout), trong
`internal/agent/migrate_persona/runtime_<name>.go`. Migrate = `read[from] →
write[to]` (`RunMigration(from, to, opts)`). Nên thêm runtime = **đúng 1 file
adapter**, tự động chạy với mọi runtime sẵn có, cả 2 chiều — số file **tuyến tính
(2/runtime)**, không phải N×(N-1) như per-pair. Đăng ký adapter vào map `adapters`
trong `migrator.go`; không cần `Direction` enum mới. Runtime không có adapter
(persona external/out-of-band, vd PicoClaw) bị `CanMigrate` bỏ qua — switch
tới/từ nó không migrate.

> **Template copy-là-chạy:** `internal/agent/migrate_persona/runtime_example.go` là
> skeleton build-ignored, comment đầy đủ — copy sang `runtime_<name>.go`, xóa dòng
> `//go:build ignore`, rồi điền 5 bước wiring + read/write. Nó ghi sẵn quyết định
> từng field (slot riêng vs inline vs fold) ngay trong comment.

Chạy lúc os-server boot sau switch thật (`Reconcile`, khi `agent_state.json`
prev ≠ current). Mỗi adapter mang gì:

- **SOUL.md** → file identity của backend. Nếu backend **không có slot IDENTITY.md
  riêng** (Hermes không có), inline các field IDENTITY đã điền của owner thành
  block `## Your identity card` trong SOUL (xem `buildIdentityBlock`).
- **MEMORY.md + `memory/*.md` daily + KNOWLEDGE.md** → merge vào file long-term
  memory của backend. **Trước hết kiểm tra backend LOAD file nào THEO TÊN** —
  Hermes chỉ load `MEMORY.md` + `USER.md` (không glob `memories/*.md`), nên một
  `KNOWLEDGE.md` riêng sẽ bị bỏ qua; ta fold nó vào `MEMORY.md`.
- **USER.md** → file user-profile của backend.
- Đặt **`Overwrite = true`** cho copy soul khi switch: switch nghĩa là "lấy persona
  vừa dùng sang". `copyPersona` backup trước (`.bak-<nano>`).
- Chiều ngược phải **strip artifact riêng của backend** mà nó đã thêm (vd identity
  card — OpenClaw giữ tên trong IDENTITY.md riêng) **VÀ khôi phục thứ chiều xuôi đã
  inline về đúng slot gốc của nó**. Inline tên vào SOUL của Hermes nhưng chiều ngược
  chỉ strip thì **mất tên** — chiều ngược phải parse card và ghi field về OpenClaw
  `IDENTITY.md` (`restoreIdentityCard`, nghịch đảo của `ensureIdentityInlined`:
  thay-hoặc-thêm dòng, giữ template sẵn có; dựa trên cùng đảm bảo "onboard không
  ghi đè IDENTITY.md đã tồn tại" mà `UpdateIdentityName` dùng). Mỗi inline ở chiều
  xuôi cần một restore tương ứng ở chiều ngược, nếu không round-trip sẽ âm thầm mất
  state.

**ĐỪNG** mang file riêng-runtime: `AGENTS.md`, `TOOLS.md`, `HEARTBEAT.md`,
`hooks/` — chúng thuộc runtime nguồn. **Bộ nhớ sâu** của backend (DB
episodic/semantic, dream-diary, grounded-short-term) **không portable** — bản
distilled `MEMORY.md`/`USER.md` mới là dạng mang đi được.

### Phân loại mỗi bước migration: fold vs move

Switch có round-trip không mất hay không là **tuỳ từng backend**, quyết định bởi
backend đích có slot nào. Đừng giả định đối xứng — phân loại mỗi bước xuôi:

- **Move / inline** (field sang slot *khác*, vd tên IDENTITY → inline vào SOUL):
  đảo ngược được, và chiều ngược **BẮT BUỘC** restore về slot gốc. Bỏ restore là âm
  thầm mất state — chính là bug mất tên. **Mỗi inline cần một restore tương ứng.**
- **Fold** (hai cấu trúc nguồn gộp vào *một* đích, vì backend đích thiếu slot cho
  một trong số đó): cố hữu mất **cấu trúc** (không mất dữ liệu), và **không có
  nghịch đảo trung thực** — đã gộp thì không tách lại được. Chỉ fold khi đích thực
  sự thiếu slot, và ghi nó như một asymmetry một-chiều đã biết **trong doc riêng
  của backend đó** (đây là tính chất của backend đó, không phải của framework
  migration — backend khác *có* slot sẽ map 1:1 và round-trip sạch).

Vậy tính đối xứng migration của một backend là fact về *slot của backend đó*, ghi
trong doc backend (vd `docs/agentic/hermes.md`), không phải đảm bảo chung ở đây.

---

## 5. Skills

- Skills tới backend bằng cách **copy** (kiểm tra: copy hay convert! `claw migrate`
  của Hermes là `shutil.copytree`, không transform) vào thư mục skill của backend.
- **Restore-sau-reset** thuộc **presync**, guard theo thư mục rỗng (để switch
  thường là no-op — không churn). Xem §3.
- **Skill watcher** (auto-update từ CDN, gate theo capability): plumbing generic
  fetch/extract/hash share ở `internal/skills/skillzip.go`
  (`FetchSkillVersions`/`DownloadToTempFile`/`FolderHash`/`ExtractSkillZip`). Thêm
  `internal/<name>/skill_watcher.go` mỏng song song với
  `internal/openclaw/skill_watcher.go` — chỉ khác **thư mục đích** và **đường
  notify**. Gate bằng `skills.Supported(device.Capabilities(...))`. Notify agent
  bằng `SendSystemChatMessage`.

---

## 6. Hooks — reimplement phía OS cho Hermes (ví dụ mẫu)

Hooks OpenClaw (`hooks/<name>/{HOOK.md, handler.ts}`) là handler TypeScript fire
trên event `message:preprocessed` của OpenClaw — `emotion-acknowledge` (mặt
"thinking" ngay khi nhận tin) và `turn-gate` (set busy cho turn từ kênh). Chúng
**riêng-runtime**, không portable. Backend mới **không** thừa kế chúng.

**Vì sao OpenClaw cần hook còn Hermes thì không.** Trong OpenClaw mọi turn chạy
bên trong daemon; os-server đẩy tin qua WebSocket rồi mất dấu, nên chỗ duy nhất để
chen "thinking" đúng thời điểm là hook. Với Hermes, điểm chặn đã nằm phía Go —
**mọi turn gửi tới Hermes đều đi qua `internal/hermes/chat.go:sendChat`** (voice,
sensing, web, và cả Telegram — receive loop nằm phía Lumi, xem `telegramRunOrigin`).
Nên ta reimplement hook native bằng Go và fire từ `sendChat`, thay vì materialize
file hook vào workspace (Hermes không có onboarding Go để làm việc đó, và không có
loader `~/.hermes/hooks/HOOK.yaml` để chạy — `handler.ts` mà `claw migrate` copy
vào là đồ chết dưới Hermes).

Đã làm:
- **`emotion-acknowledge` → Go native** trong `internal/hermes/emotion_ack.go`
  (`fireAckEmotion`, gọi từ `sendChat`). Mirror `handler.ts` 1:1: cùng emotion
  (`thinking`, intensity `0.7`), cùng skip prefix
  (`[sensing:`/`[activity]`/`[emotion]`/`[speech_emotion]` + rỗng), và cap-gate đi
  qua **registry dùng chung** `skills.SupportedHooks`
  (→ `HookCapability["emotion-acknowledge"] = expression`), resolve một lần lúc
  khởi tạo (`Service.ackHookEnabled`). Skip `[HANDLED]` của hook TS ánh xạ sang
  check Go-native `IsSilentRun(runID)` — os-server báo turn realtime-handled qua
  `MarkSilentRun`, không phải marker trong body.
- **`turn-gate` → không mirror (thừa).** `sendChat` đã set busy
  (`busySince`/`activeTurn`) trước round-trip mạng, nên gate riêng sẽ trùng lặp.

> ⚠️ **Coupling bảo trì — không có liên kết compile-time.** `hooks/emotion-acknowledge/
> handler.ts` (OpenClaw) và `internal/hermes/emotion_ack.go` (Hermes) là hai bản
> cài đặt độc lập của cùng một hành vi. **Sửa cái này phải sửa cái kia** — skip
> rules, tên/intensity emotion, và cap-gate phải y hệt, nếu không hai backend lệch
> nhau trong im lặng. Giữ comment chéo trong cả hai file.

**Khi nào *mới cần* hook native (Python).** Chỉ cho turn phát sinh **bên trong**
backend và không bao giờ qua `sendChat` — ví dụ heartbeat của backend, hoặc một
kênh backend tự lắng nghe trực tiếp (không do Lumi proxy). Lamp hiện chưa có turn
loại này, nên path Python-plugin (`pre_gateway_dispatch` trong `~/.hermes/plugins/`,
do Hermes discover; **không có** loader `~/.hermes/hooks/HOOK.yaml` trong bản ship
— verify loader thật trước khi giả định) được **hoãn theo YAGNI**, không phải gap
parity. Chỉ làm khi xuất hiện nguồn turn như vậy.

---

## 7. Factory reset

- Thêm wipe của backend ở `server/system/reset_<name>.go` (`wipe<Name>State`) và
  `case` trong `factoryreset.go`.
- **Wipe `/root/config/agent_state.json` khoá-bước với `config.json`** — chúng là
  một cặp (runtime hiện tại + lịch sử switch). Để lại `agent_state.json` trong khi
  `config.json` reset làm `prev` cũ lệch với `current` bị reset → kích **migration
  persona giả** lan state đã-wipe/stub.
- Giữ thứ phải sống (`bootstrap.json` = state OTA).
- Một wipe path xoá nội dung đã migrate (skills, config) phải có **restore path
  chạy SAU reset** (presync, §3/§5) — không thì nội dung mất luôn khi `install.sh`
  ngừng re-run.

---

## 8. Gate theo capability

Dùng metadata nền tảng runtime-agnostic trong `internal/skills`:
- `skills.Supported(deviceCaps)` cho skills, `skills.SupportedHooks(deviceCaps)`
  cho hooks, với `deviceCaps = device.Capabilities(config.DeviceTypeOrDefault())`.
- Đừng hardcode danh sách skill/hook theo backend — gate y như OpenClaw.

---

## Checklist cho backend mới

- [ ] Package `internal/<name>/`; `*Service` implement **toàn bộ** `AgentGateway`.
- [ ] Mọi stub đều `// no-op because …` hoặc `// TODO(<name>-…)` — không thân rỗng.
- [ ] `domain.AgentRuntime<Name>` + entry `AgentRuntimes`; `factory.go` case.
- [ ] `install.sh` + `install.go` (`//go:embed` + `runtimereg.Register`).
- [ ] **Setup có-state → `presync.sh`** (`//go:embed` + `runtimereg.RegisterPresync`),
      materialize bởi os-server mỗi switch. Không gì reset-fragile chỉ nằm trong
      `install.sh`.
- [ ] `verify` hook rẻ (CLI có mặt), không phải structure-check.
- [ ] `migrate_persona/runtime_<name>.go` (copy `runtime_example.go`): MỘT adapter
      read + MỘT write (bundle ↔ layout), đăng ký vào map `adapters`. `read` xuất SOUL, field identity,
      MEMORY/USER, và slot KNOWLEDGE/daily nếu có; `write` restore từng cái về slot
      gốc (identity → file riêng, hoặc inline nếu không có slot) và fold slot mà
      backend thiếu. `Overwrite=true` cho SOUL. Không cần `Direction` enum mới.
- [ ] Skills: copy-import + **restore-trong-presync** (guard) + `skill_watcher.go`
      (song song openclaw, share `internal/skills/skillzip.go`).
- [ ] Hooks: native-backend hoặc OS-side — đã quyết & ghi (không thiếu âm thầm).
      Nếu reimplement OS-side bằng Go (không liên kết compile-time với hook TS),
      thêm comment chéo trong cả hai file để sửa cái này cảnh báo cái kia.
- [ ] `reset_<name>.go` + `factoryreset.go` case; **`agent_state.json` wipe cùng
      `config.json`**.
- [ ] Gate capability qua `skills.Supported` / `SupportedHooks`.
- [ ] Notify agent khi skill đổi qua `SendSystemChatMessage`.
- [ ] Docs: cập nhật backend doc kiểu `docs/agentic/hermes.md` + checklist này nếu hợp đồng đổi.

---

## Trạng thái parity Hermes (sổ trung thực)

**Xong:** nối switch, install nhúng + presync (config self-heal, restore skills),
migrate persona/memory (SOUL + inline identity, MEMORY + daily + KNOWLEDGE, USER),
`UpdateIdentityName`, skill watcher, factory-reset khoá-bước `agent_state.json`,
hooks (`emotion-acknowledge` reimplement OS-side bằng Go; `turn-gate` thừa — §6).

**Còn mở / no-op:** hook native (Python) cho nguồn turn nội-bộ-backend (hoãn YAGNI
— §6), `WriteMCPEntry`/`RemoveMCPEntry` (`TODO(hermes-mcp)`), `CompactSession`,
`FetchChatHistory`, và nhóm model-sync (`StartModelSync`, `UpdatePrimaryModel`,
`StartPrimaryModelWatch`, `RefreshModelsConfig` — phần lớn N/A vì os-server gửi
model cố định tới custom provider campaign-api).
