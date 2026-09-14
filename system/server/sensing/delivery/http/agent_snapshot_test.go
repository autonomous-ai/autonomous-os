package http

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/gin-gonic/gin"
)

// Driven through a real engine rather than by calling the handler directly:
// gin defers WriteHeader until the engine flushes it, so a bare
// CreateTestContext leaves the recorder reading 200 whatever the handler set —
// and a 404 assertion written that way passes against every bug it exists to
// catch.
func serveSnapshot(t *testing.T, runtime, source, name string) int {
	t.Helper()
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/api/sensing/agent-snapshot/:runtime/:source/:name",
		(&SensingHandler{}).GetAgentSnapshot)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, httptest.NewRequest(http.MethodGet,
		"/api/sensing/agent-snapshot/"+runtime+"/"+source+"/"+name, nil))
	return rec.Code
}

// The runtime allow-list here has to match the one that BUILDS the URL
// (agent/delivery/http/camera_snapshot.go) and the one that decides where HAL
// writes (hal/config.py _AGENT_CONFIG_DIRS). opencode was absent from this
// list, so even a correctly built URL answered 404 — the frame existed, the
// link existed, and the image never appeared.
func TestGetAgentSnapshotServesEveryRuntimeHALWritesTo(t *testing.T) {
	home := t.TempDir()
	t.Setenv("OS_AGENT_HOME", home)

	for _, runtime := range []string{
		"openclaw", "hermes", "picoclaw", "claudecode", "opencode",
	} {
		dir := filepath.Join(home, "."+runtime, "media", "hal-snapshots")
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "snap_1.jpg"), []byte("jpeg"), 0o644); err != nil {
			t.Fatal(err)
		}
		if code := serveSnapshot(t, runtime, "media-hal-snapshots", "snap_1.jpg"); code != http.StatusOK {
			t.Errorf("%s: got status %d, want 200 — this runtime is not served", runtime, code)
		}
	}
}

// The runtime segment arrives from a URL. An unknown one must not reach the
// filesystem even when a file happens to sit there.
func TestGetAgentSnapshotRejectsAnUnknownRuntime(t *testing.T) {
	home := t.TempDir()
	t.Setenv("OS_AGENT_HOME", home)
	dir := filepath.Join(home, ".evilruntime", "media", "hal-snapshots")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "snap_1.jpg"), []byte("jpeg"), 0o644); err != nil {
		t.Fatal(err)
	}
	if code := serveSnapshot(t, "evilruntime", "media-hal-snapshots", "snap_1.jpg"); code != http.StatusNotFound {
		t.Errorf("an unlisted runtime was served: status %d", code)
	}
}

// A name is a basename, never a path. Traversal must not escape the snapshot
// directory.
func TestGetAgentSnapshotRejectsATraversingName(t *testing.T) {
	home := t.TempDir()
	t.Setenv("OS_AGENT_HOME", home)
	if err := os.WriteFile(filepath.Join(home, "secret.jpg"), []byte("jpeg"), 0o644); err != nil {
		t.Fatal(err)
	}
	if code := serveSnapshot(t, "codex", "media-hal-snapshots", "..%2f..%2fsecret.jpg"); code == http.StatusOK {
		t.Error("a traversing name escaped the snapshot directory")
	}
}
