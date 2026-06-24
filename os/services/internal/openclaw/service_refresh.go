package openclaw

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"

	"go.autonomous.ai/os/domain"
)

// RefreshChannelConfig rewrites channels.<channel> in openclaw.json using the
// canonical apply helpers, then restarts the gateway. Unlike AddChannel this
// path NEVER calls `openclaw channels add`, `plugins install`, or any other CLI
// side effect — refresh is config-only. Today only slack is implemented; other
// channels return an error so future expansion stays explicit.
//
// Returns the detected runtime version string ("Y.M.P", empty when undetected)
// so the caller can echo the runtime version in fd_channel responses, and errors
// out (without restarting) if openclaw.json does not yet exist — refresh is only
// meaningful on already-onboarded devices.
//
// The runtime is read ONCE per call from the cached OpenClaw version
// (currentOpenclawRuntime — the same value the MQTT info message reports), not a
// fresh shell-out, so refresh stays consistent with setup/add_channel.
func (s *OpenclawService) RefreshChannelConfig(ctx context.Context, req domain.RefreshChannelRequest) (string, error) {
	_ = ctx // config-only path: no subprocess to bound, ctx kept for interface symmetry.
	runtime := currentOpenclawRuntime()
	runtimeStr := runtimeVersionString(runtime)

	// Hold primarySyncMu for the full read-modify-write cycle so this cannot
	// interleave with SyncModelsFromAPI or RefreshModelsConfig writing a newer
	// version of openclaw.json between our ReadFile and our WriteFile.
	s.primarySyncMu.Lock()
	defer s.primarySyncMu.Unlock()

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	raw, err := os.ReadFile(configPath)
	if err != nil {
		return runtimeStr, fmt.Errorf("read openclaw config: %w (device must be set up first)", err)
	}
	var configData map[string]any
	if err := json.Unmarshal(raw, &configData); err != nil {
		return runtimeStr, fmt.Errorf("parse openclaw config: %w", err)
	}

	channelsMap := ensureMap(configData, "channels")
	pluginsMap := ensureMap(configData, "plugins")
	entriesMap := ensureMap(pluginsMap, "entries")

	switch req.Channel {
	case domain.ChannelSlack:
		slackMap := ensureMap(channelsMap, domain.ChannelSlack)
		applySlackChannelConfig(slackMap, slackChannelConfig{
			BotToken:      req.SlackBotToken,
			AppToken:      req.SlackAppToken,
			UserID:        req.SlackUserID,
			Mode:          req.SlackMode,
			SigningSecret: req.SlackSigningSecret,
			WebhookPath:   req.SlackWebhookPath,
			Runtime:       runtime,
		})
		channelsMap[domain.ChannelSlack] = slackMap
		slackEntryMap := ensureMap(entriesMap, domain.ChannelSlack)
		slackEntryMap["enabled"] = true
	default:
		return runtimeStr, fmt.Errorf("refresh not implemented for channel %q", req.Channel)
	}
	configData["channels"] = channelsMap
	configData["plugins"] = pluginsMap

	// Mirror AddChannel's elevated.allowFrom seed so older devices missing this
	// entry pick it up on refresh — without it, elevated tools (e.g. /exec) stay
	// gated to channels that were already in the allowFrom map.
	if toolsMap, ok := configData["tools"].(map[string]any); ok {
		if elevatedMap, ok := toolsMap["elevated"].(map[string]any); ok {
			elevatedAllowFrom := ensureMap(elevatedMap, "allowFrom")
			elevatedAllowFrom[req.Channel] = []any{"*"}
			elevatedMap["allowFrom"] = elevatedAllowFrom
		}
	}

	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return runtimeStr, fmt.Errorf("marshal openclaw config: %w", err)
	}
	// Refresh does not change the primary model — write the existing primary into
	// the flag so the watcher correctly identifies this as an os-server write.
	if existingPrimary := extractPrimaryModel(configData); existingPrimary != "" {
		setOSWriteFlag(s.config.OpenclawConfigDir, existingPrimary)
	}
	if err := os.WriteFile(configPath, written, 0600); err != nil {
		return runtimeStr, fmt.Errorf("write openclaw config: %w", err)
	}
	if err := chownRuntimeUserIfRoot(configPath, openclawRuntimeUser); err != nil {
		return runtimeStr, fmt.Errorf("set openclaw config ownership: %w", err)
	}
	slog.Info("wrote openclaw config", "component", "openclaw", "path", configPath, "channel", req.Channel, "via", "refresh")

	if err := restartOpenclawGateway(); err != nil {
		return runtimeStr, err
	}
	slog.Info("gateway restarted", "component", "openclaw", "via", "refresh")
	return runtimeStr, nil
}

// runtimeVersionString returns "Y.M.P" when the runtime was detected, "" otherwise.
// Empty signals to the backend that version probing failed (still recoverable;
// the refresh itself may have succeeded since AtLeast treats undetected as modern).
func runtimeVersionString(r RuntimeInfo) string {
	if !r.Detected {
		return ""
	}
	return fmt.Sprintf("%d.%d.%d", r.Year, r.Minor, r.Patch)
}
