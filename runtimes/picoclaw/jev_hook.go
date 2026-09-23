package picoclaw

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"go.autonomous.ai/os/runtimes/hermes"
	"go.autonomous.ai/os/system/server/config"
)

//go:embed resources/hooks/jev/hook.py
var jevHookScript []byte

// ensureJevHook reconciles assets and registration. Existing explicit opt-outs win.
func (s *PicoclawService) ensureJevHook() (bool, error) {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()
	return syncJevHook(picoclawConfigPath(), config.Path(), filepath.Join(filepath.Dir(picoclawConfigPath()), "hooks", "jev"))
}

func syncJevHook(cfgPath, osConfigPath, dir string) (bool, error) {
	cfg, err := readPicoclawConfig(cfgPath)
	if err != nil {
		return false, err
	}
	if !filepath.IsAbs(osConfigPath) {
		return false, fmt.Errorf("Jev config path must be absolute")
	}
	before, _ := json.Marshal(cfg)
	assets, err := hermes.JevSelectorAssets()
	if err != nil {
		return false, err
	}
	assets["router.py"] = bytes.ReplaceAll(assets["router.py"], []byte("[hermes-jev]"), []byte("[picoclaw-jev]"))
	assets["hook.py"] = jevHookScript
	assets["__init__.py"] = []byte{}
	assets["os-config-path.json"], _ = json.Marshal(map[string]string{"config_path": osConfigPath})
	if err := os.MkdirAll(dir, 0700); err != nil {
		return false, err
	}
	changed := false
	for name, data := range assets {
		updated, err := writePicoFileIfChanged(filepath.Join(dir, name), data, 0600)
		if err != nil {
			return changed, err
		}
		changed = changed || updated
	}
	applyJevHook(cfg, filepath.Join(dir, "hook.py"))
	after, _ := json.Marshal(cfg)
	if !bytes.Equal(before, after) {
		if err := writePicoclawConfig(cfgPath, cfg); err != nil {
			return changed, err
		}
		changed = true
	}
	return changed, nil
}

// Install a disabled registration unless the operator explicitly opted in.
// The observer owns the global hook gate; Jev must never enable it itself.
func applyJevHook(cfg map[string]any, path string) {
	hooks := ensurePicoMap(cfg, "hooks")
	processes := ensurePicoMap(hooks, "processes")
	current, _ := processes["jev"].(map[string]any)
	enabled := current["enabled"] == true
	processes["jev"] = map[string]any{"enabled": enabled, "transport": "stdio", "command": []any{"python3", path}, "intercept": []any{"before_llm"}}
}
