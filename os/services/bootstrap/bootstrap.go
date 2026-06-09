package bootstrap

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/bootstrap/config"
	"go.autonomous.ai/os/bootstrap/state"
	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/core/system"
	"go.autonomous.ai/os/lib/lelamp"
)

// semverRe captures the first semver-like token (e.g. 2026.3.8 or v1.2.3-beta).
var semverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)

// Bootstrap is the simplified OTA worker.
type Bootstrap struct {
	cfg    *config.Config
	client *http.Client
	state  *state.State
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
		allowed := map[string]bool{domain.OTAKeyLamp: true, domain.OTAKeyWeb: true, domain.OTAKeyLeLamp: true}
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

	changed := false
	// Driven by metadata.openclaw.version — bumped via scripts/release/upload-openclaw.sh.
	// detectVersion / applyUpdate already handle OTAKeyOpenClaw (npm install +
	// systemctl restart openclaw); the old reconcileOpenClawFromNpm() pulled
	// "latest" from `npm view` instead and is no longer needed.
	for _, key := range []string{domain.OTAKeyLamp, domain.OTAKeyBootstrap, domain.OTAKeyWeb, domain.OTAKeyLeLamp, domain.OTAKeyBuddy, domain.OTAKeyOpenClaw} {
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

	if changed {
		if err := state.Save(b.cfg.StateFile, b.state); err != nil {
			return fmt.Errorf("save state: %w", err)
		}
	}
	return nil
}

// reconcile compares current vs target version and applies update if needed.
func (b *Bootstrap) reconcile(ctx context.Context, key string, target domain.OTAComponent) (bool, error) {
	targetVersion := strings.TrimSpace(target.Version)
	if targetVersion == "" {
		return false, fmt.Errorf("metadata[%s].version is empty", key)
	}

	current := b.detectVersion(ctx, key)
	if current == "" {
		current = b.state.Components[key]
	}

	if current == targetVersion {
		if b.state.Components[key] != targetVersion {
			b.state.Components[key] = targetVersion
			return true, nil
		}
		return false, nil
	}

	slog.Info("update available", "component", "bootstrap", "key", key, "current", current, "target", targetVersion)

	// Status LED: orange breathing while updating
	lelamp.SetEffect("breathing", 255, 140, 0, 0.4)

	if err := b.applyUpdate(ctx, key, target); err != nil {
		lelamp.SetEffect("pulse", 255, 30, 30, 1.5) // red pulse on error
		return false, err
	}

	// Brief green flash to confirm success, then stop
	lelamp.SetEffect("notification_flash", 0, 255, 80, 1.0)
	slog.Info("updated", "component", "bootstrap", "key", key, "version", targetVersion)
	b.state.Components[key] = targetVersion
	return true, nil
}

// fetchMetadata fetches OTA metadata JSON from the configured URL.
func (b *Bootstrap) fetchMetadata(ctx context.Context) (domain.OTAMetadata, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, b.cfg.MetadataURL, nil)
	if err != nil {
		return nil, fmt.Errorf("build metadata request: %w", err)
	}
	resp, err := b.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetch metadata %s: %w", b.cfg.MetadataURL, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("fetch metadata %s: status %s", b.cfg.MetadataURL, resp.Status)
	}
	var meta domain.OTAMetadata
	if err := json.NewDecoder(resp.Body).Decode(&meta); err != nil {
		return nil, fmt.Errorf("decode metadata: %w", err)
	}
	return meta, nil
}

// detectVersion returns the current installed version for a component.
func (b *Bootstrap) detectVersion(ctx context.Context, key string) string {
	runCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()

	switch key {
	case domain.OTAKeyLamp:
		out, err := system.Run(runCtx, "lamp-server", "--version")
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
	case domain.OTAKeyLeLamp:
		path := filepath.Join("/opt/hal", "VERSION_LELAMP")
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
		return openclawNormalizeVersion(string(out))
	default:
		return ""
	}
}

// applyUpdate runs the appropriate update command for the given component.
func (b *Bootstrap) applyUpdate(ctx context.Context, key string, component domain.OTAComponent) error {
	switch key {
	case domain.OTAKeyLamp, domain.OTAKeyWeb, domain.OTAKeyLeLamp, domain.OTAKeyBuddy, domain.OTAKeyOpenClaw:
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

// openclawNormalizeVersion extracts the version from openclaw --version output (e.g. "OpenClaw 2026.3.8 (3caab92)" -> "2026.3.8").
// Used only for OTAKeyOpenClaw.
func openclawNormalizeVersion(raw string) string {
	line := strings.TrimSpace(strings.TrimRight(raw, "\r\n"))
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	if loc := semverRe.FindStringSubmatch(line); len(loc) > 1 {
		return loc[1]
	}
	return ""
}

// normalizeVersion extracts a semver-like version from command output (e.g. "1.0.83" or "lamp-server 1.0.83" -> "1.0.83").
// Used for OTAKeyLamp and bootstrap-style version output (lamp-server --version, bootstrap-server --version).
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
