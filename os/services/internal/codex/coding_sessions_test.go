package codex

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// writeRollout writes a minimal codex rollout JSONL (session_meta + a user
// response_item) under sessionsDir/2026/07/08/rollout-<id>.jsonl and stamps its
// mtime so recency ordering is deterministic.
func writeRollout(t *testing.T, sessionsDir, threadID, cwd, summary string, mtime time.Time) {
	t.Helper()
	dir := filepath.Join(sessionsDir, "2026", "07", "08")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	path := filepath.Join(dir, "rollout-2026-07-08T00-00-00-"+threadID+".jsonl")
	meta := `{"type":"session_meta","payload":{"id":` + js(threadID) + `,"cwd":` + js(cwd) + `}}` + "\n"
	user := `{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":` + js(summary) + `}]}}` + "\n"
	if err := os.WriteFile(path, []byte(meta+user), 0o644); err != nil {
		t.Fatalf("write rollout: %v", err)
	}
	if err := os.Chtimes(path, mtime, mtime); err != nil {
		t.Fatalf("chtimes: %v", err)
	}
}

func js(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}

func TestAllCodingSessionsAndFolders(t *testing.T) {
	dir := t.TempDir()
	now := time.Now()
	writeRollout(t, dir, "aaaa1111", "/root/test", "caro game", now.Add(-2*time.Hour))
	writeRollout(t, dir, "bbbb2222", "/root", "old work", now.Add(-3*time.Hour))
	writeRollout(t, dir, "cccc3333", "/root", "new work", now.Add(-30*time.Minute))

	s := &CodexService{codexSessionsDirPath: dir}

	all := s.allCodingSessions()
	if len(all) != 3 {
		t.Fatalf("allCodingSessions = %d, want 3", len(all))
	}
	if all[0].SessionID != "cccc3333" || all[0].Folder != "/root" || all[0].label() != "new work" {
		t.Errorf("newest = %+v, want cccc3333 /root 'new work'", all[0])
	}

	folders := s.codingFolders()
	if len(folders) != 2 {
		t.Fatalf("codingFolders = %d, want 2", len(folders))
	}
	if folders[0].Folder != "/root" || folders[0].SessionID != "cccc3333" {
		t.Errorf("folders[0] = %+v, want /root newest cccc3333", folders[0])
	}

	rootSessions := s.folderSessions("/root")
	if len(rootSessions) != 2 || rootSessions[0].SessionID != "cccc3333" {
		t.Fatalf("folderSessions(/root) = %+v", rootSessions)
	}

	latest, ok := s.latestSessionForFolder("/root/test")
	if !ok || latest.SessionID != "aaaa1111" {
		t.Errorf("latestSessionForFolder(/root/test) = %+v ok=%v", latest, ok)
	}
	if _, ok := s.latestSessionForFolder("/root/nope"); ok {
		t.Errorf("latestSessionForFolder(/root/nope) should be absent")
	}
}

func TestReadRolloutMetaSkipsNonUser(t *testing.T) {
	dir := t.TempDir()
	// A rollout whose first response_item is a developer message (not user) must
	// still recover thread/cwd, and the summary must come from the user line.
	subdir := filepath.Join(dir, "2026", "07", "08")
	if err := os.MkdirAll(subdir, 0o755); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(subdir, "rollout-2026-07-08T00-00-00-dddd4444.jsonl")
	body := `{"type":"session_meta","payload":{"id":"dddd4444","cwd":"/root/app"}}` + "\n" +
		`{"type":"response_item","payload":{"type":"message","role":"developer","content":[{"type":"input_text","text":"instructions"}]}}` + "\n" +
		`{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"fix login bug"}]}}` + "\n"
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	s := &CodexService{codexSessionsDirPath: dir}
	sessions := s.folderSessions("/root/app")
	if len(sessions) != 1 {
		t.Fatalf("want 1 session, got %d", len(sessions))
	}
	if sessions[0].label() != "fix login bug" {
		t.Errorf("summary = %q, want user text (developer line skipped)", sessions[0].label())
	}
}

func TestReadRolloutMetaSkipsInjectedContext(t *testing.T) {
	dir := t.TempDir()
	subdir := filepath.Join(dir, "2026", "07", "08")
	if err := os.MkdirAll(subdir, 0o755); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(subdir, "rollout-2026-07-08T00-00-00-eeee5555.jsonl")
	// First user message is codex's injected environment_context; the real
	// prompt follows and must be the summary.
	body := `{"type":"session_meta","payload":{"id":"eeee5555","cwd":"/root/test_codex"}}` + "\n" +
		`{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"<environment_context>\n<cwd>/root/test_codex</cwd>\n<shell>bash</shell>\n</environment_context>"}]}}` + "\n" +
		`{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"build a snake game"}]}}` + "\n"
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	s := &CodexService{codexSessionsDirPath: dir}
	sessions := s.folderSessions("/root/test_codex")
	if len(sessions) != 1 {
		t.Fatalf("want 1 session, got %d", len(sessions))
	}
	if sessions[0].label() != "build a snake game" {
		t.Errorf("summary = %q, want real prompt (env-context skipped)", sessions[0].label())
	}
}

func TestIsInjectedContext(t *testing.T) {
	injected := []string{
		"<environment_context>\n<cwd>/x</cwd>",
		"  <system-reminder>hi</system-reminder>",
		"<user_instructions>do x</user_instructions>",
		"",
	}
	for _, s := range injected {
		if !isInjectedContext(s) {
			t.Errorf("isInjectedContext(%q) = false, want true", s)
		}
	}
	real := []string{"build a snake game", "fix the <div> tag in index.html", "add /health route"}
	for _, s := range real {
		if isInjectedContext(s) {
			t.Errorf("isInjectedContext(%q) = true, want false", s)
		}
	}
}

func TestNormalizeFolder(t *testing.T) {
	cases := map[string]string{
		"/root/test":     "/root/test",
		"/root/test/":    "/root/test",
		"test":           "/root/test",
		"~/proj":         "/root/proj",
		"~":              "/root",
		`"/root/a b"`:    "/root/a b",
		"/root/./x/../y": "/root/y",
	}
	for in, want := range cases {
		if got := normalizeFolder(in); got != want {
			t.Errorf("normalizeFolder(%q) = %q, want %q", in, got, want)
		}
	}
	if got := normalizeFolder("   "); got != "" {
		t.Errorf("normalizeFolder(blank) = %q, want empty", got)
	}
}
