package server

import (
	"strings"
	"testing"

	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/server/config"
)

func TestWakeGreetingPromptIncludesActiveRuntime(t *testing.T) {
	i18n.SetConfig(&config.Config{STTLanguage: i18n.LangVI})

	prompt := wakeGreetingPrompt("Codex", "lamp", map[string]bool{
		"vision": true,
		"audio":  true,
		"motion": true,
	})
	if !strings.Contains(prompt, "[context: current_language=vi]") {
		t.Errorf("wake greeting missing language context: %q", prompt)
	}
	if !strings.Contains(prompt, "[context: agent_runtime=Codex]") {
		t.Errorf("wake greeting missing active runtime context: %q", prompt)
	}
	if !strings.Contains(prompt, "[context: device_type=lamp]") {
		t.Errorf("wake greeting missing device type context: %q", prompt)
	}
	if !strings.Contains(prompt, "[context: device_capabilities=audio,motion,vision]") {
		t.Errorf("wake greeting missing sorted device capabilities context: %q", prompt)
	}
}
