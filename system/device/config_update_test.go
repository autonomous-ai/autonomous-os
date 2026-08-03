package device

import (
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// baseConfig returns a config prefilled the way a set-up device looks, so
// PATCH-semantics tests can assert that untouched fields survive.
func baseConfig() *config.Config {
	return &config.Config{
		LLMAPIKey:         "key-llm",
		LLMBaseURL:        "https://api.example.com",
		LLMModel:          "model-a",
		DeepgramAPIKey:    "key-dg",
		STTAPIKey:         "key-stt",
		TTSAPIKey:         "key-tts",
		STTBaseURL:        "https://stt.example.com",
		TTSBaseURL:        "https://tts.example.com",
		STTLanguage:       "en",
		STTModel:          "flux-general-en",
		TTSProvider:       "openai",
		TTSVoice:          "alloy",
		NetworkSSID:       "home-wifi",
		NetworkPassword:   "wifi-pass",
		Channel:           domain.ChannelTelegram,
		TelegramBotToken:  "tg-token",
		TelegramUserID:    "tg-user",
		MQTTEndpoint:      "mqtt.example.com",
		AdminPasswordHash: "old-hash",
	}
}

func TestApplyUpdateEmptyRequestChangesNothing(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{}, "")

	if ch.model || ch.thinking || ch.baseURL || ch.wifi || ch.lang || ch.voice || ch.realtime || ch.channel {
		t.Fatalf("empty request set flags: %+v", ch)
	}
	if c.LLMAPIKey != "key-llm" || c.TTSVoice != "alloy" || c.TelegramBotToken != "tg-token" ||
		c.NetworkSSID != "home-wifi" || c.AdminPasswordHash != "old-hash" {
		t.Fatalf("empty request mutated config: %+v", c)
	}
}

func TestApplyUpdateModelOnly(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{LLMModel: "model-b"}, "")

	if !ch.model || ch.newModel != "model-b" {
		t.Fatalf("model change not flagged: %+v", ch)
	}
	// llm_model is not in hal's boot-read set — must not bounce TTS.
	if ch.voice || ch.thinking || ch.baseURL {
		t.Fatalf("model-only change set unrelated flags: %+v", ch)
	}

	// Re-sending the active model is a no-op.
	ch = applyUpdate(c, domain.UpdateConfigRequest{LLMModel: "model-b"}, "")
	if ch.model {
		t.Fatal("re-sending current model flagged as change")
	}
}

func TestApplyUpdateLLMKeyTriggersVoiceRestart(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{LLMAPIKey: "key-new"}, "")
	if !ch.voice {
		t.Fatal("llm_api_key change must flag voice (hal reads it at boot)")
	}
	if ch.model || ch.baseURL {
		t.Fatalf("llm_api_key change set unrelated flags: %+v", ch)
	}
}

func TestApplyUpdateBaseURL(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{LLMBaseURL: "https://api2.example.com"}, "")
	if !ch.baseURL || !ch.voice {
		t.Fatalf("base_url change not flagged: %+v", ch)
	}
	// Re-sending the same URL (post-normalization) is a no-op.
	ch = applyUpdate(c, domain.UpdateConfigRequest{LLMBaseURL: "https://api2.example.com"}, "")
	if ch.baseURL || ch.voice {
		t.Fatalf("identical base_url flagged as change: %+v", ch)
	}
}

func TestApplyUpdateThinkingFlagFollowsPresence(t *testing.T) {
	c := baseConfig()
	v := true
	ch := applyUpdate(c, domain.UpdateConfigRequest{LLMDisableThinking: &v}, "")
	if !ch.thinking {
		t.Fatal("thinking pointer sent but not flagged")
	}
	if c.LLMDisableThinking == nil || !*c.LLMDisableThinking {
		t.Fatal("thinking value not persisted")
	}
}

func TestApplyUpdateLanguageDerivesModel(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{STTLanguage: "vi"}, "")
	if !ch.lang || ch.prevLang != "en" || ch.newLang != "vi" {
		t.Fatalf("language change not flagged: %+v", ch)
	}
	if c.STTModel != "nova-3-general" {
		t.Fatalf("stt model not derived: %s", c.STTModel)
	}
	// Same language again → no session reset, no hal restart.
	ch = applyUpdate(c, domain.UpdateConfigRequest{STTLanguage: "vi"}, "")
	if ch.lang {
		t.Fatal("re-sending current language flagged as change")
	}
}

func TestApplyUpdateWifi(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{SSID: "new-wifi", Password: "new-pass"}, "")
	if !ch.wifi || ch.newSSID != "new-wifi" || ch.newPassword != "new-pass" {
		t.Fatalf("wifi change not captured: %+v", ch)
	}
	// Same SSID → no reconnect, even with a password present.
	ch = applyUpdate(c, domain.UpdateConfigRequest{SSID: "new-wifi", Password: "other-pass"}, "")
	if ch.wifi {
		t.Fatal("same-ssid save flagged wifi reconnect")
	}
	if c.NetworkPassword != "other-pass" {
		t.Fatal("password not persisted on same-ssid save")
	}
}

func TestApplyUpdateChannelTokens(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{TelegramBotToken: "tg-token-2"}, "")
	if !ch.channel {
		t.Fatal("token change not flagged for gateway re-push")
	}
	// chanReq must carry the full post-save credential set, not just the delta.
	if ch.chanReq.Channel != domain.ChannelTelegram || ch.chanReq.TelegramBotToken != "tg-token-2" || ch.chanReq.TelegramUserID != "tg-user" {
		t.Fatalf("chanReq incomplete: %+v", ch.chanReq)
	}

	// Switching channel entirely also re-pushes; untouched telegram creds survive.
	ch = applyUpdate(c, domain.UpdateConfigRequest{Channel: domain.ChannelSlack, SlackBotToken: "sl-bot", SlackUserID: "sl-user"}, "")
	if !ch.channel || ch.chanReq.Channel != domain.ChannelSlack || ch.chanReq.SlackBotToken != "sl-bot" {
		t.Fatalf("channel switch not captured: %+v", ch.chanReq)
	}
	if c.TelegramBotToken != "tg-token-2" {
		t.Fatal("channel switch wiped telegram creds")
	}
}

func TestApplyUpdateMQTTOnlySkipsRestarts(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{MQTTEndpoint: "mqtt2.example.com", MQTTPort: 8883}, "")
	if ch.voice || ch.lang || ch.realtime || ch.channel || ch.wifi || ch.model {
		t.Fatalf("mqtt-only save set restart flags: %+v", ch)
	}
	if c.MQTTEndpoint != "mqtt2.example.com" || c.MQTTPort != 8883 {
		t.Fatalf("mqtt fields not persisted: %+v", c)
	}
}

func TestApplyUpdateAdminHash(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{}, "new-hash")
	if c.AdminPasswordHash != "new-hash" {
		t.Fatal("admin hash not replaced")
	}
	if ch.voice || ch.channel {
		t.Fatalf("admin-only save set restart flags: %+v", ch)
	}
}

func TestApplyUpdateRealtime(t *testing.T) {
	c := baseConfig()
	enabled := true
	wakeWord := true
	ch := applyUpdate(c, domain.UpdateConfigRequest{Realtime: &domain.RealtimeSetData{Enabled: &enabled}, WakeWord: &wakeWord}, "")
	if !ch.realtime {
		t.Fatal("realtime payload not flagged for hal restart")
	}
	if c.Realtime == nil || c.Realtime.Enabled == nil || !*c.Realtime.Enabled {
		t.Fatal("realtime enabled not persisted")
	}
	if c.WakeWord == nil || !*c.WakeWord {
		t.Fatal("wakeword not persisted")
	}
}

func TestApplyUpdateWakeWordRestartsHAL(t *testing.T) {
	c := baseConfig()
	wakeWord := true
	ch := applyUpdate(c, domain.UpdateConfigRequest{WakeWord: &wakeWord}, "")
	if !ch.voice {
		t.Fatal("wakeword-only save must restart HAL")
	}
	if c.WakeWord == nil || !*c.WakeWord {
		t.Fatal("wakeword not persisted")
	}
}

func TestApplyWakeWord(t *testing.T) {
	c := baseConfig()
	if !applyWakeWord(c, true) {
		t.Fatal("false to true must report a change")
	}
	if c.WakeWord == nil || !*c.WakeWord {
		t.Fatal("wakeword true not persisted")
	}
	if applyWakeWord(c, true) {
		t.Fatal("re-applying true must not report a change")
	}
	if !applyWakeWord(c, false) {
		t.Fatal("true to false must report a change")
	}
}
