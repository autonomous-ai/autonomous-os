package device

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const soulRobotMD = `---
schema: autonomous.device.v1
capabilities:
  audio: { routes: [audio], required: true }
soul_ref: %REF%
---

# Lamp
`

func writeSoulDevice(t *testing.T, deviceType, ref, soul string) string {
	t.Helper()
	root := t.TempDir()
	t.Setenv("DEVICES_DIR", root)
	dir := filepath.Join(root, deviceType)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	body := strings.Replace(soulRobotMD, "%REF%", ref, 1)
	if err := os.WriteFile(filepath.Join(dir, "ROBOT.md"), []byte(body), 0o644); err != nil {
		t.Fatalf("write ROBOT.md: %v", err)
	}
	if soul != "" {
		if err := os.WriteFile(filepath.Join(dir, ref), []byte(soul), 0o644); err != nil {
			t.Fatalf("write soul: %v", err)
		}
	}
	return dir
}

func TestResolveSoul_ReadsDeclaredPath(t *testing.T) {
	writeSoulDevice(t, "lamp", "SOUL.md", "# Lamp\n\nYou are a lamp.\n")

	content, hasSoul, err := ResolveSoul("lamp")
	if err != nil {
		t.Fatalf("ResolveSoul: %v", err)
	}
	if !hasSoul {
		t.Fatal("hasSoul = false, want true for a declared soul_ref")
	}
	if !strings.Contains(string(content), "You are a lamp.") {
		t.Fatalf("soul content not returned: %q", content)
	}
}

func TestResolveSoul_NoRefIsNotAnError(t *testing.T) {
	root := t.TempDir()
	t.Setenv("DEVICES_DIR", root)
	dir := filepath.Join(root, "intern-v2")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	md := "---\nschema: autonomous.device.v1\ncapabilities:\n  audio: { routes: [audio] }\n---\n"
	if err := os.WriteFile(filepath.Join(dir, "ROBOT.md"), []byte(md), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}

	content, hasSoul, err := ResolveSoul("intern-v2")
	if err != nil {
		t.Fatalf("a soulless body must not error: %v", err)
	}
	if hasSoul || content != nil {
		t.Fatalf("hasSoul = %v, content = %q; want false/nil", hasSoul, content)
	}
}

// A body that names a soul it did not ship is a deploy fault, not a soulless
// body — it must surface rather than boot with no persona.
func TestResolveSoul_DeclaredButMissingErrors(t *testing.T) {
	writeSoulDevice(t, "lamp", "SOUL.md", "")

	if _, hasSoul, err := ResolveSoul("lamp"); err == nil {
		t.Fatalf("want an error for an unresolvable soul_ref, got hasSoul=%v", hasSoul)
	}
}

func TestResolveSoul_RejectsUnsupportedScheme(t *testing.T) {
	writeSoulDevice(t, "lamp", "s3://bucket/SOUL.md", "")

	_, _, err := ResolveSoul("lamp")
	if err == nil || !strings.Contains(err.Error(), "unsupported soul_ref scheme") {
		t.Fatalf("want an unsupported-scheme error, got %v", err)
	}
}

// Every runtime wraps the resolved soul in a block that ends at the first bare
// `---` line, so a soul containing one would be truncated on disk and the
// remainder left behind as unmanaged text on the next refresh. The format is
// shared across runtimes, so this guards the shipped souls once for all of them.
func TestShippedSoulsHaveNoBareSeparator(t *testing.T) {
	robots := repoRobotsDir(t)
	entries, err := os.ReadDir(robots)
	if err != nil {
		t.Fatalf("read %s: %v", robots, err)
	}
	checked := 0
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		path := filepath.Join(robots, e.Name(), "SOUL.md")
		body, err := os.ReadFile(path)
		if err != nil {
			continue // a soulless body ships no SOUL.md
		}
		checked++
		for i, line := range strings.Split(string(body), "\n") {
			if strings.TrimSpace(line) == "---" {
				t.Errorf("%s:%d is a bare `---` separator — it would truncate the OS-managed soul block", path, i+1)
			}
		}
	}
	if checked == 0 {
		t.Fatalf("no SOUL.md found under %s — test is not checking anything", robots)
	}
}

// repoRobotsDir walks up from the test working dir to the committed robots/
// tree, so moving this package cannot silently break the check.
func repoRobotsDir(t *testing.T) string {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for dir := wd; dir != filepath.Dir(dir); dir = filepath.Dir(dir) {
		candidate := filepath.Join(dir, "robots")
		if st, err := os.Stat(filepath.Join(candidate, "lamp")); err == nil && st.IsDir() {
			return candidate
		}
	}
	t.Fatalf("robots/ tree not found above %s", wd)
	return ""
}
