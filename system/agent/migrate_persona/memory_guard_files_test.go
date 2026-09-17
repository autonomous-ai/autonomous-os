package migratepersona

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

func TestGuardMemoryFileLeavesCleanFileAlone(t *testing.T) {
	_, path := seedDevice(t, cleanUserMD, "long")
	before, _ := os.Stat(path)
	act, err := GuardMemoryFile(path, map[string]bool{"long": true}, true)
	if err != nil || act != nil {
		t.Fatalf("clean file: want no action, got %+v err=%v", act, err)
	}
	after, _ := os.Stat(path)
	if !after.ModTime().Equal(before.ModTime()) {
		t.Error("clean file must not be rewritten (prompt-cache miss)")
	}
	if _, err := os.Stat(QuarantinePath(path)); !os.IsNotExist(err) {
		t.Error("no sidecar for a clean file")
	}
}

func TestGuardMemoryFileQuarantinesWithBackupAndSidecar(t *testing.T) {
	_, path := seedDevice(t, cleanUserMD+greenLampPoison, "long")
	act, err := GuardMemoryFile(path, map[string]bool{"long": true}, true)
	if err != nil {
		t.Fatalf("guard: %v", err)
	}
	if act == nil || !act.Written || len(act.Dropped) != 1 {
		t.Fatalf("want one written action with one drop, got %+v", act)
	}
	if got := readFile(t, path); got != cleanUserMD {
		t.Errorf("file not cleaned:\n%s", got)
	}
	baks, _ := filepath.Glob(path + ".bak-*")
	if len(baks) != 1 {
		t.Errorf("want one backup, got %v", baks)
	}
	side := readFile(t, QuarantinePath(path))
	if !strings.Contains(side, "(free-prose)") || !strings.Contains(side, "Obsidian") {
		t.Errorf("sidecar must carry reason and text:\n%s", side)
	}
}

func TestGuardMemoryFileObserveOnlyWritesNothing(t *testing.T) {
	raw := cleanUserMD + greenLampPoison
	_, path := seedDevice(t, raw, "long")
	act, err := GuardMemoryFile(path, map[string]bool{"long": true}, false)
	if err != nil || act == nil || act.Written || len(act.Dropped) != 1 {
		t.Fatalf("observe mode: want reported-not-written, got %+v err=%v", act, err)
	}
	if readFile(t, path) != raw {
		t.Error("observe mode must not touch the file")
	}
}

func TestGuardMemoryFilePicksRuleByName(t *testing.T) {
	dir := t.TempDir()
	mem := filepath.Join(dir, "MEMORY.md")
	// Free prose is fine in MEMORY.md; a curl prescription is not.
	body := "- Long likes jazz\n- Full-room scan works best as curl-driven aim + look per direction\n"
	if err := os.WriteFile(mem, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	act, err := GuardMemoryFile(mem, nil, true)
	if err != nil || act == nil || len(act.Dropped) != 1 {
		t.Fatalf("want the curl line dropped, got %+v err=%v", act, err)
	}
	if readFile(t, mem) != "- Long likes jazz\n" {
		t.Errorf("wrong result:\n%s", readFile(t, mem))
	}
}

func TestGuardMemoryFileAbsentIsNoop(t *testing.T) {
	act, err := GuardMemoryFile(filepath.Join(t.TempDir(), "USER.md"), nil, true)
	if err != nil || act != nil {
		t.Fatalf("absent file: want nil,nil got %+v %v", act, err)
	}
}

func TestMemoryFilePathsCoverEveryAdapter(t *testing.T) {
	opts := Options{
		OpenclawWorkspace: "/o/workspace", HermesRoot: "/h", PicoclawWorkspace: "/p/workspace",
		CodexWorkspace: "/c/workspace", ClaudecodeWorkspace: "/cc/workspace", OpenCodeWorkspace: "/oc/workspace",
	}.withDefaults()
	got := MemoryFilePaths(opts)
	if len(got) != len(adapters) {
		t.Fatalf("want one MEMORY.md per adapter (%d), got %v", len(adapters), got)
	}
	for _, want := range []string{"/h/memories/MEMORY.md", "/p/workspace/memory/MEMORY.md", "/o/workspace/MEMORY.md"} {
		found := false
		for _, p := range got {
			found = found || p == want
		}
		if !found {
			t.Errorf("missing %s in %v", want, got)
		}
	}
}

// TestQuarantinePathNeverEndsInMarkdown pins the sidecar extension. PicoClaw's
// MEMORY.md lives in `<ws>/memory/`, and HAL's OpenClawContextManager globs
// `memory/*.md` (hal/realtime/context_manager/openclaw.py, load_device_memory
// and summarize_device_memory, newest first). A `.md` sidecar there would feed
// every quarantined block straight back into the realtime session and its
// device_summary.md — the exact poison the guard removed.
func TestQuarantinePathNeverEndsInMarkdown(t *testing.T) {
	p := "/home/pi/.picoclaw/workspace/memory/MEMORY.md"
	got := QuarantinePath(p)
	if strings.HasSuffix(got, ".md") {
		t.Fatalf("sidecar %q must not end in .md: HAL globs memory/*.md", got)
	}
	if !strings.HasPrefix(got, p+".quarantine") {
		t.Errorf("sidecar must sit next to its file, got %q", got)
	}
}

// TestGuardMemoryFilePrunesOldBackups covers F4: every trip writes a
// `.bak-<nano>`, and heartbeat churn made that ~50 files a day. Only the
// newest guardBackupsKept survive, and the one just written is among them.
func TestGuardMemoryFilePrunesOldBackups(t *testing.T) {
	_, path := seedDevice(t, cleanUserMD+greenLampPoison, "long")
	// Seven stale backups with ascending numeric suffixes, all older than
	// anything the guard will write now (UnixNano is ~1.7e18).
	for i := 1; i <= 7; i++ {
		name := fmt.Sprintf("%s.bak-%d", path, 1000+i)
		if err := os.WriteFile(name, []byte("old"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	act, err := GuardMemoryFile(path, map[string]bool{"long": true}, true)
	if err != nil || act == nil || !act.Written {
		t.Fatalf("guard must trip and write, got %+v err=%v", act, err)
	}
	baks, _ := filepath.Glob(path + ".bak-*")
	if len(baks) != guardBackupsKept {
		t.Fatalf("want exactly %d backups after prune, got %d: %v", guardBackupsKept, len(baks), baks)
	}
	sort.Strings(baks)
	// Survivors are the newest: fake 1004..1007 plus the fresh one. The
	// numeric-suffix sort must not be fooled by string order.
	for _, want := range []string{"1004", "1005", "1006", "1007"} {
		if _, err := os.Stat(path + ".bak-" + want); err != nil {
			t.Errorf("newest fake backup .bak-%s must survive: %v", want, err)
		}
	}
	for _, gone := range []string{"1001", "1002", "1003"} {
		if _, err := os.Stat(path + ".bak-" + gone); !os.IsNotExist(err) {
			t.Errorf("oldest fake backup .bak-%s must be pruned", gone)
		}
	}
	fresh := 0
	for _, b := range baks {
		if data, _ := os.ReadFile(b); string(data) != "old" {
			fresh++
		}
	}
	if fresh != 1 {
		t.Errorf("the just-written backup must be among the survivors, found %d fresh", fresh)
	}
}
