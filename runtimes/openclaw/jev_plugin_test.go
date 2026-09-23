package openclaw

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestJevPluginSyncPreservesOptOutAndIsIdempotent(t *testing.T) {
	home := t.TempDir()
	file := filepath.Join(home, "openclaw.json")
	original := []byte(`{"plugins":{"entries":{"autonomous-jev":{"enabled":false,"config":{"enabled":false}}}}}`)
	if err := os.WriteFile(file, original, 0600); err != nil {
		t.Fatal(err)
	}
	changed, err := syncJevPlugin(home, "/private/config.json")
	if err != nil || !changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	if changed, err = syncJevPlugin(home, "/private/config.json"); err != nil || changed {
		t.Fatalf("second changed=%v err=%v", changed, err)
	}
	got, _ := os.ReadFile(file)
	if string(got) != string(original) {
		t.Fatal("operator config changed")
	}
	pointer, _ := os.ReadFile(filepath.Join(home, "extensions", jevPluginID, "os-config-path.json"))
	if string(pointer) != `{"config_path":"/private/config.json"}` {
		t.Fatalf("pointer=%s", pointer)
	}
}

func TestJevPreloadRunCorrelation(t *testing.T) {
	data, _ := json.Marshal(map[string]any{"version": 1, "skill": "harness-use", "path": "/workspace/skills/harness-use/SKILL.md", "content": "complete skill"})
	prefix := "[jev-skill-preload]\n" + string(data) + "\n[/jev-skill-preload]\n\n"
	s := &OpenclawService{}
	s.SetPendingChatTrace("run", "make report")
	if got := s.MatchPendingByMessage(prefix + "make report"); got != "run" {
		t.Fatalf("run=%q", got)
	}
	for _, text := range []string{"[jev-skill-preload]\ninvalid\n[/jev-skill-preload]\n\nhello", "[jev-skill-preload]\n" + strings.Repeat("x", 65537) + "\n[/jev-skill-preload]\n\nhello"} {
		if got := stripJevPreload(text); got != text {
			t.Fatal("invalid envelope stripped")
		}
	}
}
