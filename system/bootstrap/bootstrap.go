package bootstrap

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/bootstrap/config"
	"go.autonomous.ai/os/system/bootstrap/state"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/core/system"
	"go.autonomous.ai/os/system/lib/hal"
)

// semverRe captures the first semver-like token (e.g. 2026.3.8 or v1.2.3-beta).
var semverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)

// versionParts extracts the numeric dotted core (e.g. 1.2.3 → [1 2 3]) of a
// version string, ignoring any pre-release/build suffix. Returns nil when no
// semver-like token is present (treated as the lowest possible version).
func versionParts(v string) []int {
	core := semverRe.FindString(v)
	if core == "" {
		return nil
	}
	if i := strings.IndexAny(core, "-+_"); i >= 0 {
		core = core[:i]
	}
	var out []int
	for _, p := range strings.Split(core, ".") {
		n, err := strconv.Atoi(p)
		if err != nil {
			break
		}
		out = append(out, n)
	}
	return out
}

// compareVersions returns -1 if a < b, 0 if equal, 1 if a > b, comparing the
// numeric dotted core of each. An empty/unparseable version sorts lowest, so a
// device with an unknown current version always falls below any real floor.
func compareVersions(a, b string) int {
	pa, pb := versionParts(a), versionParts(b)
	n := len(pa)
	if len(pb) > n {
		n = len(pb)
	}
	for i := 0; i < n; i++ {
		var x, y int
		if i < len(pa) {
			x = pa[i]
		}
		if i < len(pb) {
			y = pb[i]
		}
		if x < y {
			return -1
		}
		if x > y {
			return 1
		}
	}
	return 0
}

// Bootstrap is the simplified OTA worker.
type Bootstrap struct {
	cfg    *config.Config
	client *http.Client
	state  *state.State
	// announcedThisCycle prevents the "device is updating" TTS cue from firing
	// once per component when a single check-cycle needs to update multiple
	// (e.g. HAL + web + os-server all behind min_version). Reset at the top of
	// every checkOnce so a later cycle that finds new updates re-announces.
	announcedThisCycle bool
	// security records the last metadata fetch outcome for GET /security.
	security securityTracker
}

// configRetryInterval is how often Serve reloads bootstrap.json while waiting for
// it to provide a metadata URL (i.e. the device is not yet provisioned).
const configRetryInterval = 30 * time.Second

// ProvideServer creates a Bootstrap from config. The metadata URL may be empty
// here (device not yet provisioned); Serve waits for it before polling.
func ProvideServer() (*Bootstrap, error) {
	cfg := config.LoadOrDefault()
	st, err := state.Load(cfg.StateFile)
	if err != nil {
		return nil, fmt.Errorf("load state: %w", err)
	}
	return &Bootstrap{
		cfg:    cfg,
		client: &http.Client{Timeout: 20 * time.Second},
		state:  st,
	}, nil
}

// waitForConfig blocks until bootstrap.json yields a non-empty metadata URL,
// reloading /root/config/bootstrap.json on configRetryInterval. It runs before
// any other goroutine starts, so reassigning b.cfg here is race-free. Returns
// false if ctx is cancelled (shutdown) before a URL appears.
func (b *Bootstrap) waitForConfig(ctx context.Context) bool {
	for strings.TrimSpace(b.cfg.MetadataURL) == "" {
		slog.Warn("waiting for metadata_url in bootstrap config (device not provisioned yet)",
			"component", "bootstrap", "path", "/root/config/bootstrap.json")
		select {
		case <-ctx.Done():
			return false
		case <-time.After(configRetryInterval):
		}
		b.cfg = config.LoadOrDefault()
	}
	return true
}

// Serve runs the gin HTTP server as the main loop, with OTA checks in a background goroutine.
// Handles SIGINT/SIGTERM for graceful shutdown.
func (b *Bootstrap) Serve() error {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	// The device may not be provisioned yet: wait until bootstrap.json provides a
	// metadata URL before starting the poll loop and healthcheck server.
	if !b.waitForConfig(ctx) {
		return nil
	}

	pollInterval, err := time.ParseDuration(b.cfg.PollInterval)
	if err != nil {
		return fmt.Errorf("parse poll interval: %w", err)
	}
	slog.Info("bootstrap started", "component", "bootstrap", "metadataURL", b.cfg.MetadataURL, "interval", b.cfg.PollInterval)

	// Run OTA check loop in background.
	go b.checkLoop(ctx, pollInterval)

	// Gin healthcheck as main serve.
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.GET("/health", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"status": "ok"})
	})
	r.GET("/security", func(c *gin.Context) {
		c.JSON(http.StatusOK, b.securityStatus())
	})
	r.GET("/versions", func(c *gin.Context) {
		c.JSON(http.StatusOK, b.versionReport(c.Request.Context()))
	})
	r.POST("/force-check", func(c *gin.Context) {
		go func() {
			if err := b.checkOnce(context.Background()); err != nil {
				slog.Error("force check failed", "component", "bootstrap", "error", err)
			}
		}()
		c.JSON(http.StatusOK, gin.H{"status": "ok", "message": "update check triggered"})
	})
	r.POST("/force-check/:target", func(c *gin.Context) {
		target := c.Param("target")
		// The agent CLIs are here so the web Versions card can force a check for
		// the runtime a device runs; os-server resolves its virtual "agent"
		// target to one of these before forwarding. componentInstalled still
		// decides whether the check does anything (wrong runtime / old on-device
		// updater → skipped), so a stray call cannot push a CLI onto a device
		// that does not run it.
		allowed := map[string]bool{
			domain.OTAKeyOSServer: true, domain.OTAKeyWeb: true, domain.OTAKeyHal: true,
			domain.OTAKeyCodex: true, domain.OTAKeyClaudeCode: true, domain.OTAKeyOpenCode: true, domain.OTAKeyPicoClaw: true,
		}
		if !allowed[target] {
			c.JSON(http.StatusBadRequest, gin.H{"error": "unknown target: " + target})
			return
		}
		go func() {
			if err := b.checkComponent(context.Background(), target); err != nil {
				slog.Error("force check failed", "component", "bootstrap", "target", target, "error", err)
			}
		}()
		c.JSON(http.StatusOK, gin.H{"status": "ok", "message": "update check triggered", "target": target})
	})

	port := b.cfg.HttpPort
	srv := &http.Server{Addr: fmt.Sprintf("127.0.0.1:%d", port), Handler: r}
	go func() {
		<-ctx.Done()
		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer shutdownCancel()
		_ = srv.Shutdown(shutdownCtx)
	}()
	slog.Info("healthcheck listening", "component", "bootstrap", "port", port)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		return fmt.Errorf("healthcheck server: %w", err)
	}
	return nil
}

// checkLoop runs OTA checks on a ticker in the background.
func (b *Bootstrap) checkLoop(ctx context.Context, pollInterval time.Duration) {
	if err := b.checkOnce(ctx); err != nil {
		slog.Error("initial check failed", "component", "bootstrap", "error", err)
	}

	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := b.checkOnce(ctx); err != nil {
				slog.Error("check failed", "component", "bootstrap", "error", err)
			}
		}
	}
}

// checkComponent fetches metadata and reconciles a single named component.
func (b *Bootstrap) checkComponent(ctx context.Context, key string) error {
	meta, err := b.fetchMetadata(ctx)
	if err != nil {
		return err
	}
	component, ok := meta[key]
	if !ok {
		return fmt.Errorf("component %q not found in metadata", key)
	}
	updated, err := b.reconcile(ctx, key, component)
	if err != nil {
		return err
	}
	if updated {
		if err := state.Save(b.cfg.StateFile, b.state); err != nil {
			return fmt.Errorf("save state: %w", err)
		}
	}
	return nil
}

// checkOnce fetches metadata and reconciles all components.
func (b *Bootstrap) checkOnce(ctx context.Context) error {
	meta, err := b.fetchMetadata(ctx)
	if err != nil {
		return err
	}
	if len(meta) == 0 {
		slog.Warn("empty metadata", "component", "bootstrap", "url", b.cfg.MetadataURL)
		return nil
	}

	// Reset per-cycle so a later cycle that finds new updates can announce
	// again. Without this reset the operator would only hear the cue once per
	// bootstrap-process lifetime, and long-running boxes would go silent even
	// on real updates.
	b.announcedThisCycle = false

	changed := false
	// Driven by metadata.openclaw.version — bumped via scripts/release/upload-openclaw.sh.
	// detectVersion / applyUpdate already handle OTAKeyOpenClaw (npm install +
	// systemctl restart openclaw); the old reconcileOpenClawFromNpm() pulled
	// "latest" from `npm view` instead and is no longer needed.
	// The agent-runtime CLIs (codex/claudecode/opencode/picoclaw) ride the same
	// loop; componentInstalled gates each to the runtime the device actually
	// runs. Hermes is intentionally NOT here — see domain.OTAKeyCodex's comment.
	for _, key := range []string{
		domain.OTAKeyOSServer, domain.OTAKeyBootstrap, domain.OTAKeyWeb, domain.OTAKeyHal, domain.OTAKeyBuddy,
		domain.OTAKeyOpenClaw, domain.OTAKeyCodex, domain.OTAKeyClaudeCode, domain.OTAKeyOpenCode, domain.OTAKeyPicoClaw,
	} {
		component, ok := meta[key]
		if !ok {
			continue
		}
		updated, err := b.reconcile(ctx, key, component)
		if err != nil {
			slog.Error("reconcile error", "component", "bootstrap", "key", key, "error", err)
			continue
		}
		if updated {
			changed = true
		}
	}

	// Device profile (devices.<type>) is nested in metadata, not a flat
	// component, so it can't ride the loop above — reconcile it separately.
	if updated, err := b.reconcileDevice(ctx); err != nil {
		slog.Error("device reconcile error", "component", "bootstrap", "error", err)
	} else if updated {
		changed = true
	}

	if changed {
		if err := state.Save(b.cfg.StateFile, b.state); err != nil {
			return fmt.Errorf("save state: %w", err)
		}
	}
	return nil
}

// resolveSTTLanguage returns the device's configured `stt_language` code (e.g.
// "vi", "en", "zh") so announceUpdateStart can pick the right phrase. Returns
// "" when config is missing / empty; callers fall back to English.
func resolveSTTLanguage() string {
	data, err := os.ReadFile("/root/config/config.json")
	if err != nil {
		return ""
	}
	var c struct {
		STTLanguage string `json:"stt_language"`
	}
	if json.Unmarshal(data, &c) != nil {
		return ""
	}
	return strings.TrimSpace(c.STTLanguage)
}

// otaUpdateStartPhrase returns the localized "device is updating, please wait"
// announcement text. Mirrors the language branches in HAL's
// _factory_reset_phrase — same fixed 3-lang set (vi / zh / en default) so both
// destructive-ish flows sound consistent. Text is hardcoded here rather than
// pushed through HAL i18n because bootstrap runs as its own binary and does not
// import the HAL Python module; adding a new i18n key for one phrase would
// mean touching both sides for every language change. Keep the phrases short
// (~2-3s each) so the announce + settle window fits inside the LED "orange
// breathing" cue before the actual update work drowns out further speech.
func otaUpdateStartPhrase(lang string) string {
	switch {
	case strings.HasPrefix(lang, "vi"):
		return "Thiết bị đang cập nhật, sẽ mất một chút thời gian, vui lòng chờ trong khi cập nhật."
	case strings.HasPrefix(lang, "zh"):
		return "设备正在更新，需要一点时间,请稍候。"
	default:
		return "Device is updating. This will take a moment, please wait."
	}
}

// announceUpdateStart speaks the "device is updating" cue via HAL's cached TTS
// path. Idempotent per check-cycle via b.announcedThisCycle so batched updates
// (HAL + web + os-server all behind min_version) only trigger one cue.
// Fire-and-forget: any error is logged but does not block the OTA (HAL could
// be restarting during a HAL-component update, or the device may have no
// speaker at all — audio out is a nice-to-have, not a hard requirement).
// Skipped on devices without the `audio` capability so silent-body devices
// don't waste render/network cycles on TTS that would go nowhere.
func (b *Bootstrap) announceUpdateStart() {
	if b.announcedThisCycle {
		return
	}
	b.announcedThisCycle = true
	if !device.Has(resolveDeviceType(), device.CapAudio) {
		return
	}
	phrase := otaUpdateStartPhrase(resolveSTTLanguage())
	slog.Info("OTA update cue", "component", "bootstrap", "phrase", phrase)
	if err := hal.SpeakCached(phrase); err != nil {
		slog.Warn("OTA update cue speak failed", "component", "bootstrap", "error", err)
	}
}

// progressLED shows an OTA-progress status by name (ota_progress/ota_error/
// ota_success); HAL owns the color/effect via STATUS_LED_PRESETS (per-device
// overridable). Only on a body with an LED — a device with no `light` capability
// has no /led route at all, so skip the POST. Fail-open when the device type is
// unresolved (device.Has returns true), matching legacy behavior.
func (b *Bootstrap) progressLED(state string) {
	if device.Has(resolveDeviceType(), device.CapLight) {
		hal.SetStatus(state)
	}
}

// restoreLED returns a completed transient OTA cue to the user's LED state,
// or to the ambient resting look when no user state exists.
func (b *Bootstrap) restoreLED() {
	if device.Has(resolveDeviceType(), device.CapLight) {
		hal.RestoreLED()
	}
}

// resolveDeviceType returns this device's class for picking devices.<type> in
// OTA metadata: DEVICE_TYPE env → config.json device_type. Returns "" when
// unresolved — NO "lamp" fallback (callers skip the device-profile OTA rather
// than pull the wrong device's profile).
func resolveDeviceType() string {
	if t := strings.TrimSpace(os.Getenv("DEVICE_TYPE")); t != "" {
		return t
	}
	if data, err := os.ReadFile("/root/config/config.json"); err == nil {
		var c struct {
			DeviceType string `json:"device_type"`
		}
		if json.Unmarshal(data, &c) == nil && strings.TrimSpace(c.DeviceType) != "" {
			return strings.TrimSpace(c.DeviceType)
		}
	}
	return ""
}

// fetchDeviceComponent reads metadata.devices.<type>. The profile is nested, so
// the flat OTAMetadata decode in fetchMetadata can't see it — fetch + decode the
// devices map directly.
func (b *Bootstrap) fetchDeviceComponent(ctx context.Context, deviceType string) (domain.OTAComponent, bool, error) {
	payload, verified, err := b.fetchMetadataPayload(ctx)
	if err != nil {
		return domain.OTAComponent{}, false, err
	}
	var wrap struct {
		Devices map[string]domain.OTAComponent `json:"devices"`
	}
	if err := json.Unmarshal(payload, &wrap); err != nil {
		return domain.OTAComponent{}, false, fmt.Errorf("decode verified metadata: %w", err)
	}
	if verified {
		if err := validateOTAMetadata(domain.OTAMetadata(wrap.Devices)); err != nil {
			return domain.OTAComponent{}, false, err
		}
	}
	comp, ok := wrap.Devices[deviceType]
	return comp, ok, nil
}

// reconcileDevice updates this device's profile (devices.<type>) to the metadata
// version, delegating the install to `software-update device`. Absent artifact
// for this device type → no-op (the device simply has no published profile).
func (b *Bootstrap) reconcileDevice(ctx context.Context) (bool, error) {
	deviceType := resolveDeviceType()
	if deviceType == "" {
		slog.Warn("device_type unresolved — skipping device-profile OTA (set DEVICE_TYPE; refusing to assume lamp)", "component", "bootstrap")
		return false, nil
	}
	comp, ok, err := b.fetchDeviceComponent(ctx, deviceType)
	if err != nil {
		return false, err
	}
	if !ok || strings.TrimSpace(comp.Version) == "" {
		return false, nil
	}
	return b.reconcile(ctx, domain.OTAKeyDevice, comp)
}

// reconcile decides whether the automatic OTA worker should update a component.
//
// The worker only rolls a device UP TO the approved floor (target.MinVersion,
// defaulting to target.Version when unset): it applies an update only when the
// current version is strictly BELOW that floor. A release can therefore bump
// Version without auto-pushing it — the fleet moves only once MinVersion is
// promoted. Manual `software-update <key>` over SSH bypasses this entirely and
// always installs Version (it self-fetches metadata and ignores MinVersion).
func (b *Bootstrap) reconcile(ctx context.Context, key string, target domain.OTAComponent) (bool, error) {
	targetVersion := strings.TrimSpace(target.Version)
	if targetVersion == "" {
		return false, fmt.Errorf("metadata[%s].version is empty", key)
	}
	minVersion := strings.TrimSpace(target.MinVersion)
	if minVersion == "" {
		minVersion = targetVersion
	}
	if b.cfg != nil && strings.TrimSpace(b.cfg.RollbackVersions[key]) == targetVersion {
		slog.Warn("update blocked after local rollback", "component", "bootstrap", "key", key, "version", targetVersion)
		return false, nil
	}

	current := b.detectVersion(ctx, key)
	if current == "" {
		current = b.state.Components[key]
	}

	// A component this device does not have is not "out of date" — it is simply
	// not part of this device. Metadata lists everything published; no device
	// runs all of it (a Reachy Mini has no claude-desktop-buddy, a device on a
	// non-OpenClaw runtime has no openclaw). For those, detectVersion returns ""
	// which sorts below every floor, so without this gate the worker announces
	// "device is updating" over the speaker, turns the strip orange, fails to
	// install something the device was never meant to run — and repeats every
	// poll, forever.
	//
	// Gated on componentInstalled rather than on the empty version alone so
	// self-repair still works: an os-server binary that is present but whose
	// --version is broken reports "" too, and that one must still be updated.
	if current == "" && !b.componentInstalled(key) {
		slog.Debug("component not installed on this device — skipping", "component", "bootstrap", "key", key)
		return false, nil
	}

	// At or above the approved floor → nothing to auto-apply. Keep persisted
	// state in sync with what's actually installed.
	if compareVersions(current, minVersion) >= 0 {
		// A newer build exists but the approved floor holds it back — surface it
		// so staged rollouts are visible (promote min_version to release it).
		if compareVersions(current, targetVersion) < 0 {
			slog.Info("update held by min_version floor", "component", "bootstrap", "key", key, "current", current, "min", minVersion, "target", targetVersion)
		}
		if current != "" && b.state.Components[key] != current {
			b.state.Components[key] = current
			return true, nil
		}
		return false, nil
	}

	slog.Info("update available", "component", "bootstrap", "key", key, "current", current, "min", minVersion, "target", targetVersion)

	// Voice cue BEFORE the LED + apply so the user hears "device is updating"
	// while the strip is still on the current color and speech isn't fighting
	// a HAL restart that a HAL-component update would trigger seconds later.
	// Idempotent per cycle (b.announcedThisCycle) — a batched OS-server+HAL+web
	// update speaks once, not thrice.
	b.announceUpdateStart()

	// Status LED: orange breathing while updating
	b.progressLED("ota_progress")

	if err := b.applyUpdate(ctx, key, target); err != nil {
		b.progressLED("ota_error") // red pulse on error
		return false, err
	}

	// Brief green flash to confirm success, then restore the user's chosen
	// look (or the ambient resting look if none exists). The flash lasts about
	// 750ms at its preset speed, so wait a full second before restoring.
	b.progressLED("ota_success")
	time.Sleep(time.Second)
	b.restoreLED()
	// The bootstrap updater replaces this process asynchronously. Do not record
	// the target as deployed until a later poll observes the restarted binary's
	// injected version; otherwise a failed self-update would be persisted as a
	// success and suppress the retry/rollback operator path.
	if key == domain.OTAKeyBootstrap {
		slog.Info("bootstrap update staged; waiting for restarted version confirmation", "component", "bootstrap", "version", targetVersion)
		return false, nil
	}
	slog.Info("updated", "component", "bootstrap", "key", key, "version", targetVersion)
	b.state.Components[key] = targetVersion
	return true, nil
}

// fetchMetadata fetches OTA metadata JSON from the configured URL.
func (b *Bootstrap) fetchMetadata(ctx context.Context) (domain.OTAMetadata, error) {
	payload, verified, err := b.fetchMetadataPayload(ctx)
	if err != nil {
		return nil, err
	}
	return decodeOTAMetadataPayload(payload, verified)
}

func (b *Bootstrap) fetchMetadataPayload(ctx context.Context) (payload []byte, verified bool, err error) {
	// Every outcome — including a transport failure before any verification
	// could run — lands in the security status, so an operator polling
	// GET /security sees a stalled feed instead of a stale success.
	defer func() { b.security.record(verified, err) }()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, b.cfg.MetadataURL, nil)
	if err != nil {
		return nil, false, fmt.Errorf("build metadata request: %w", err)
	}
	resp, err := b.client.Do(req)
	if err != nil {
		return nil, false, fmt.Errorf("fetch metadata %s: %w", b.cfg.MetadataURL, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, false, fmt.Errorf("fetch metadata %s: status %s", b.cfg.MetadataURL, resp.Status)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return nil, false, fmt.Errorf("read metadata: %w", err)
	}
	if strings.TrimSpace(b.cfg.SigningPublicKey) == "" {
		slog.Warn("OTA signature verification disabled: signing_public_key is not provisioned", "component", "bootstrap")
		return data, false, nil
	}
	payload, err = verifyOTAMetadata(data, b.cfg.SigningPublicKey)
	if err != nil {
		return nil, false, fmt.Errorf("verify metadata %s: %w", b.cfg.MetadataURL, err)
	}
	return payload, true, nil
}

func decodeOTAMetadataPayload(payload []byte, requireChecksums bool) (domain.OTAMetadata, error) {
	var meta domain.OTAMetadata
	if err := json.Unmarshal(payload, &meta); err != nil {
		return nil, fmt.Errorf("decode verified metadata payload: %w", err)
	}
	if requireChecksums {
		if err := validateOTAMetadata(meta); err != nil {
			return nil, err
		}
	}
	return meta, nil
}

// detectVersion returns the current installed version for a component.
func (b *Bootstrap) detectVersion(ctx context.Context, key string) string {
	runCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()

	switch key {
	case domain.OTAKeyOSServer:
		out, err := system.Run(runCtx, "os-server", "--version")
		if err != nil {
			return ""
		}
		return normalizeVersion(string(out))
	case domain.OTAKeyBootstrap:
		return strings.TrimSpace(config.BootstrapVersion)
	case domain.OTAKeyWeb:
		path := filepath.Join("/usr/share/nginx/html/setup", "VERSION")
		data, err := os.ReadFile(path)
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(data))
	case domain.OTAKeyHal:
		path := filepath.Join("/opt/hal", "VERSION_HAL")
		data, err := os.ReadFile(path)
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(data))
	case domain.OTAKeyBuddy:
		path := filepath.Join("/opt/claude-desktop-buddy", "VERSION_BUDDY")
		data, err := os.ReadFile(path)
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(data))
	case domain.OTAKeyOpenClaw:
		out, err := system.Run(runCtx, "openclaw", "--version")
		if err != nil {
			return ""
		}
		return cliSemver(string(out))
	case domain.OTAKeyCodex:
		// "codex-cli 0.142.5" — the metadata carries the bare semver.
		out, err := system.Run(runCtx, "codex", "--version")
		if err != nil {
			return ""
		}
		return cliSemver(string(out))
	case domain.OTAKeyClaudeCode:
		// "2.1.218 (Claude Code)".
		out, err := system.Run(runCtx, "claude", "--version")
		if err != nil {
			return ""
		}
		return cliSemver(string(out))
	case domain.OTAKeyOpenCode:
		out, err := system.Run(runCtx, "opencode", "--version")
		if err != nil {
			return ""
		}
		return cliSemver(string(out))
	case domain.OTAKeyPicoClaw:
		// Deliberately NOT `picoclaw version`: that prints a build description
		// ("nightly-44-g1959045c-dirty") with no relation to the release tag, so
		// it would never parse and the component would look infinitely stale.
		// `software-update picoclaw` stamps the tag it installed here instead.
		data, err := os.ReadFile(domain.PicoClawVersionStamp)
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(data))
	case domain.OTAKeyDevice:
		dir := os.Getenv("DEVICES_DIR")
		if dir == "" {
			dir = "/opt/devices"
		}
		data, err := os.ReadFile(filepath.Join(dir, resolveDeviceType(), "VERSION"))
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(data))
	default:
		return ""
	}
}

// componentInstalled reports whether this component exists on the device at all.
//
// Deliberately coarser than detectVersion: it asks "is the artifact here", not
// "which version is here". A present-but-unreadable install therefore still
// counts as installed and can be repaired by an OTA; only a component that is
// genuinely absent — the metadata offers it, this device never had it — is
// skipped.
//
// The lookups mirror detectVersion and the on-device updater
// (robots/<type>/software-update); keep the three in step. os-server/openclaw
// use the same PATH resolution detectVersion runs them with, so the two can
// never disagree about whether the binary exists.
func (b *Bootstrap) componentInstalled(key string) bool {
	switch key {
	case domain.OTAKeyBootstrap:
		// This process. Always — otherwise the worker could never self-update.
		return true
	case domain.OTAKeyOSServer:
		return inPath("os-server")
	case domain.OTAKeyOpenClaw:
		// Absent on devices running another agent runtime (hermes, codex,
		// claudecode, …). Those must not be dragged onto OpenClaw by the OTA.
		// Left on binary presence deliberately: openclaw is npm-installed per
		// device rather than baked into every image, so the check is meaningful
		// here — and an unset agent_runtime (older provisioning) must not stop
		// OpenClaw devices from updating.
		return inPath("openclaw")
	case domain.OTAKeyCodex, domain.OTAKeyClaudeCode, domain.OTAKeyOpenCode, domain.OTAKeyPicoClaw:
		// Binary presence proves NOTHING for these: scripts/imager/build-orangepi.sh
		// bakes every agent CLI onto every lamp/intern-v2 image regardless of
		// DEFAULT_AGENT. An inPath() check would therefore mark all four
		// "installed" on every device, and each poll would announce "device is
		// updating" over the speaker, turn the strip orange, download the CLI,
		// and restart a unit that does not exist — forever.
		//
		// The runtime the device actually runs is the real predicate.
		//
		// Second gate: the on-device updater must know the key. `software-update`
		// reaches a device ONLY via the imager or setup.sh (see scripts/README.md)
		// — never over OTA — so a device provisioned before these keys existed
		// keeps an updater that answers "Unknown app: codex" forever. Without this
		// check that device would, every poll (5m): speak "device is updating",
		// breathe orange, fail the apply, and latch the LED red (the error path
		// does not restoreLED). Skipping instead means such devices simply never
		// receive agent-CLI updates — which is the only outcome available to them
		// anyway — and do it silently.
		return resolveAgentRuntime() == key && updaterSupports(key)
	case domain.OTAKeyWeb:
		return dirExists("/usr/share/nginx/html/setup")
	case domain.OTAKeyHal:
		return dirExists("/opt/hal")
	case domain.OTAKeyBuddy:
		return dirExists("/opt/claude-desktop-buddy")
	case domain.OTAKeyDevice:
		dir := os.Getenv("DEVICES_DIR")
		if dir == "" {
			dir = "/opt/devices"
		}
		deviceType := resolveDeviceType()
		if deviceType == "" {
			return false
		}
		return dirExists(filepath.Join(dir, deviceType))
	default:
		return false
	}
}

// resolveAgentRuntime returns the agent runtime this device runs, from
// `agent_runtime` in /root/config/config.json (the same file os-server writes
// when the user switches runtimes in the web UI). Returns "" when the file or
// key is missing — callers treat that as "not this runtime", which is the safe
// direction: an unknown runtime skips the CLI update instead of pushing one.
//
// Values match the domain.OTAKey* constants for the CLIs by construction.
func resolveAgentRuntime() string {
	data, err := os.ReadFile("/root/config/config.json")
	if err != nil {
		return ""
	}
	var c struct {
		AgentRuntime string `json:"agent_runtime"`
	}
	if json.Unmarshal(data, &c) != nil {
		return ""
	}
	return strings.TrimSpace(c.AgentRuntime)
}

// updaterSupports reports whether the on-device `software-update` script has a
// branch for this component key.
//
// It matches the branch guard verbatim — `[ "$APP" = "<key>" ]`, the exact form
// every branch in scripts/provision/software-update uses — rather than looking
// for the key anywhere in the file: the key also appears in comments and in the
// usage strings of an updater that does NOT implement it, so a loose search
// would report support that isn't there.
//
// A missing/unreadable script means "no support": the caller then skips the
// component, which is strictly better than exec'ing an updater that will fail.
func updaterSupports(key string) bool {
	path, err := exec.LookPath("software-update")
	if err != nil {
		return false
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	return strings.Contains(string(data), `[ "$APP" = "`+key+`" ]`)
}

func inPath(name string) bool {
	_, err := exec.LookPath(name)
	return err == nil
}

func dirExists(path string) bool {
	fi, err := os.Stat(path)
	return err == nil && fi.IsDir()
}

// applyUpdate runs the appropriate update command for the given component.
func (b *Bootstrap) applyUpdate(ctx context.Context, key string, component domain.OTAComponent) error {
	switch key {
	case domain.OTAKeyOSServer, domain.OTAKeyWeb, domain.OTAKeyHal, domain.OTAKeyBuddy, domain.OTAKeyOpenClaw, domain.OTAKeyDevice,
		domain.OTAKeyCodex, domain.OTAKeyClaudeCode, domain.OTAKeyOpenCode, domain.OTAKeyPicoClaw:
		// All non-bootstrap components delegate to the on-device
		// `software-update <key>` script (installed by setup.sh) so the
		// install logic lives in one place — the script self-fetches
		// metadata.json and handles each app's specifics (npm install
		// for openclaw, zip-extract + systemctl restart for the rest).
		runCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
		defer cancel()
		out, err := system.Run(runCtx, "software-update", key)
		if err != nil {
			return fmt.Errorf("software-update %s: %w", key, err)
		}
		slog.Info("update output", "component", "bootstrap", "key", key, "output", out)
		return nil

	case domain.OTAKeyBootstrap:
		// Spawn as detached background process so it survives bootstrap exit.
		slog.Info("spawning background software-update bootstrap", "component", "bootstrap")
		if err := system.SpawnBackground("software-update", "bootstrap"); err != nil {
			return fmt.Errorf("spawn software-update bootstrap: %w", err)
		}
		return nil

	default:
		return fmt.Errorf("unsupported component %q", key)
	}
}

// cliSemver extracts the semver from the FIRST line of an agent CLI's
// --version output: "OpenClaw 2026.3.8 (3caab92)" -> "2026.3.8",
// "codex-cli 0.142.5" -> "0.142.5", "2.1.218 (Claude Code)" -> "2.1.218".
// Shared by openclaw/codex/claudecode/opencode — every one of them prints the
// version somewhere on line one, and each publishes that bare semver as its
// metadata version. NOT usable for picoclaw (no semver in its output).
func cliSemver(raw string) string {
	line := strings.TrimSpace(strings.TrimRight(raw, "\r\n"))
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	if loc := semverRe.FindStringSubmatch(line); len(loc) > 1 {
		return loc[1]
	}
	return ""
}

// normalizeVersion extracts a semver-like version from command output (e.g. "1.0.83" or "os-server 1.0.83" -> "1.0.83").
// Used for OTAKeyOSServer and bootstrap-style version output (os-server --version, bootstrap-server --version).
func normalizeVersion(raw string) string {
	line := strings.TrimSpace(strings.TrimRight(raw, "\r\n"))
	if line == "" {
		return ""
	}
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	if loc := semverRe.FindStringSubmatch(line); len(loc) > 1 {
		return loc[1]
	}
	fields := strings.Fields(line)
	if len(fields) == 0 {
		return ""
	}
	return strings.TrimSpace(fields[len(fields)-1])
}
