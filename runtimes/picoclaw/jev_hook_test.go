package picoclaw

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestJevHookReconcile(t *testing.T) {
	dir := t.TempDir()
	cfg := filepath.Join(dir, "config.json")
	if err := os.WriteFile(cfg, []byte(`{"unrelated":true}`), 0600); err != nil {
		t.Fatal(err)
	}
	hookDir := filepath.Join(dir, "hooks", "jev")
	changed, err := syncJevHook(cfg, filepath.Join(dir, "os.json"), hookDir)
	if err != nil || !changed {
		t.Fatalf("first: %v %v", changed, err)
	}
	changed, err = syncJevHook(cfg, filepath.Join(dir, "os.json"), hookDir)
	if err != nil || changed {
		t.Fatalf("second: %v %v", changed, err)
	}
	raw, err := os.ReadFile(filepath.Join(hookDir, "os-config-path.json"))
	if err != nil {
		t.Fatal(err)
	}
	var pointer map[string]string
	json.Unmarshal(raw, &pointer)
	if len(pointer) != 1 || pointer["config_path"] != filepath.Join(dir, "os.json") {
		t.Fatal(pointer)
	}
}

func TestJevHookOptOut(t *testing.T) {
	for _, raw := range []string{`{"hooks":{"enabled":false}}`, `{"hooks":{"enabled":true,"processes":{"jev":{"enabled":false}}}}`} {
		var cfg map[string]any
		json.Unmarshal([]byte(raw), &cfg)
		applyJevHook(cfg, "hook.py")
		hooks := cfg["hooks"].(map[string]any)
		if hooks["enabled"] == false {
			continue
		}
		if hooks["processes"].(map[string]any)["jev"].(map[string]any)["enabled"] != false {
			t.Fatal(cfg)
		}
	}
}

// Existing observer onboarding owns the global hook gate. Jev's explicit
// per-plugin opt-out survives that lifecycle without disabling the observer.
func TestJevOptOutSurvivesObserverOnboarding(t *testing.T) {
	cfg := map[string]any{"hooks": map[string]any{"enabled": false, "processes": map[string]any{"jev": map[string]any{"enabled": false}}}}
	applyObserverHook(cfg, "observer.py", "localhost")
	applyJevHook(cfg, "hook.py")
	hooks := cfg["hooks"].(map[string]any)
	if hooks["enabled"] != true || hooks["processes"].(map[string]any)["jev"].(map[string]any)["enabled"] != false {
		t.Fatal(cfg)
	}
}

func TestJevHookBuildSwitchOverridesConfig(t *testing.T) {
	for _, raw := range []string{`{}`, `{"hooks":{"enabled":true}}`, `{"hooks":{"processes":{"jev":{}}}}`, `{"hooks":{"processes":{"jev":{"enabled":"true"}}}}`, `{"hooks":{"enabled":true,"processes":{"jev":{"enabled":true}}}}`} {
		var cfg map[string]any
		if err := json.Unmarshal([]byte(raw), &cfg); err != nil {
			t.Fatal(err)
		}
		want := jevEnabled
		applyJevHook(cfg, "hook.py")
		hooks := cfg["hooks"].(map[string]any)
		if hooks["processes"].(map[string]any)["jev"].(map[string]any)["enabled"] != want {
			t.Fatal(cfg)
		}
		applyObserverHook(cfg, "observer.py", "localhost")
		applyJevHook(cfg, "hook.py")
		if hooks["processes"].(map[string]any)["jev"].(map[string]any)["enabled"] != want {
			t.Fatal("onboarding changed opt-in", cfg)
		}
	}
}
