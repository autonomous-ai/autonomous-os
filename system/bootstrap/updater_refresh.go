package bootstrap

import (
	"context"
	"crypto/sha256"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path"
	"path/filepath"
)

// The on-device `software-update` script is NOT an OTA component: nothing in
// metadata.json describes it, so a device keeps whatever copy its image shipped
// with, forever. Every fix to the updater therefore needed an operator to SSH in
// and run the curl/`bash -n`/install one-liner from docs/bootstrap-ota.md by
// hand — on every device.
//
// Bootstrap is the right place to automate that, and the script itself is the
// wrong one. Bash reads a script lazily as it executes, so a running
// `software-update` that overwrote its own path would start executing the new
// bytes at whatever offset it had reached. Bootstrap, by contrast, refreshes the
// file strictly BETWEEN runs, and — being an OTA component itself — can still be
// fixed remotely if this logic ever needs to change.
//
// Failure is never fatal here: a device that cannot reach the network, or that
// is served a truncated file, keeps the updater it already had. The one outcome
// this must never produce is a device with no working updater.

// updaterPath is where the provisioning scripts install the updater. A var, not
// a const, purely so tests can point the install at a temp directory instead of
// writing to a real /usr/local/bin.
var updaterPath = "/usr/local/bin/software-update"

// updaterURLFrom derives the published updater URL from the metadata URL, so
// there is no second knob to configure and no way for the two to point at
// different releases. `make upload-setup` publishes the raw script one level
// above the OTA namespace:
//
//	{base}/ota/metadata.json  →  {base}/software-update
func updaterURLFrom(metadataURL string) (string, error) {
	u, err := url.Parse(metadataURL)
	if err != nil {
		return "", fmt.Errorf("parse metadata url: %w", err)
	}
	if u.Scheme == "" || u.Host == "" {
		return "", fmt.Errorf("metadata url %q is not absolute", metadataURL)
	}
	// /os/ota/metadata.json → /os
	base := path.Dir(path.Dir(u.Path))
	if base == "." || base == "/" {
		return "", fmt.Errorf("metadata url %q has no namespace to derive from", metadataURL)
	}
	u.Path = path.Join(base, "software-update")
	u.RawQuery = ""
	u.Fragment = ""
	return u.String(), nil
}

// updateInFlight reports whether a force update is installing right now.
//
// The automatic path needs no such check — refreshUpdater is called at the top
// of checkOnce, on the same goroutine that later execs the updater, so those two
// can never overlap. A force update arrives on an HTTP handler's goroutine
// instead, and that one genuinely can land mid-poll.
func updateInFlight() bool {
	busy := false
	inFlight.Range(func(_, _ any) bool {
		busy = true
		return false
	})
	return busy
}

// refreshUpdater brings /usr/local/bin/software-update up to the published copy.
//
// Call it only between updater runs — see the package comment above. It is a
// no-op when the bytes already match, so the steady state costs one conditional
// GET per poll and no disk write.
func (b *Bootstrap) refreshUpdater(ctx context.Context) {
	// An update in progress means the script is currently executing. Replacing
	// it now is exactly the lazy-read hazard this design avoids; the next poll
	// will pick it up.
	if updateInFlight() {
		return
	}

	current, err := os.ReadFile(updaterPath)
	if err != nil {
		// No updater on this device at all: that is a provisioning problem, not
		// something to fix by dropping an unrequested root-owned script in.
		return
	}

	src, err := updaterURLFrom(b.cfg.MetadataURL)
	if err != nil {
		slog.Debug("updater refresh: no source url", "component", "bootstrap", "error", err)
		return
	}

	published, err := b.fetchUpdater(ctx, src)
	if err != nil {
		slog.Debug("updater refresh: fetch failed", "component", "bootstrap", "url", src, "error", err)
		return
	}

	if sha256.Sum256(published) == sha256.Sum256(current) {
		return
	}

	if err := installUpdater(ctx, published); err != nil {
		slog.Warn("updater refresh: keeping the existing updater", "component", "bootstrap", "error", err)
		return
	}
	slog.Info("updater refreshed", "component", "bootstrap", "url", src, "bytes", len(published))
}

// fetchUpdater downloads the published script. The size cap is a guard against
// a proxy or captive portal serving something enormous in place of the script,
// which would otherwise be read entirely into memory on a device with 1-2 GB.
func (b *Bootstrap) fetchUpdater(ctx context.Context, src string) ([]byte, error) {
	const maxUpdaterBytes = 1 << 20 // 1 MiB; the real script is ~40 KB

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, src, nil)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	// The updater is republished in place, so a cached copy would hide the very
	// fix this exists to deliver.
	req.Header.Set("Cache-Control", "no-cache")

	resp, err := b.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetch %s: %w", src, err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("fetch %s: status %s", src, resp.Status)
	}

	data, err := io.ReadAll(io.LimitReader(resp.Body, maxUpdaterBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", src, err)
	}
	if len(data) > maxUpdaterBytes {
		return nil, fmt.Errorf("fetch %s: larger than %d bytes", src, maxUpdaterBytes)
	}
	if len(data) == 0 {
		return nil, fmt.Errorf("fetch %s: empty body", src)
	}
	return data, nil
}

// installUpdater validates the downloaded script and puts it in place.
//
// Two properties matter and both are load-bearing:
//
//   - `bash -n` runs BEFORE anything replaces the live file. A truncated
//     download is syntactically broken far more often than not, and a device
//     whose updater no longer parses cannot be repaired remotely.
//   - the temp file is written in the SAME directory and renamed, so the
//     replacement is atomic. A copy-in-place would leave a window where
//     /usr/local/bin/software-update is half-written, and that is precisely the
//     window in which bootstrap or an operator might exec it.
func installUpdater(ctx context.Context, data []byte) error {
	dir := filepath.Dir(updaterPath)

	tmp, err := os.CreateTemp(dir, ".software-update.new-*")
	if err != nil {
		return fmt.Errorf("create temp: %w", err)
	}
	tmpName := tmp.Name()
	// Any early return below must not leave the staging file behind; after a
	// successful rename the path no longer exists and Remove is a harmless
	// no-op.
	defer func() { _ = os.Remove(tmpName) }()

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp: %w", err)
	}
	// CreateTemp makes the file 0600; the updater is exec'd, so it needs 0755
	// before the rename rather than after (the rename must publish a file that
	// is already runnable).
	if err := os.Chmod(tmpName, 0o755); err != nil {
		return fmt.Errorf("chmod temp: %w", err)
	}

	if out, err := exec.CommandContext(ctx, "bash", "-n", tmpName).CombinedOutput(); err != nil {
		return fmt.Errorf("downloaded updater failed bash -n: %w: %s", err, out)
	}

	if err := os.Rename(tmpName, updaterPath); err != nil {
		return fmt.Errorf("install: %w", err)
	}
	return nil
}
