package claudecode

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync"

	"go.autonomous.ai/os/domain"
)

// telegramTargetsFile is the Device-owned store of known Telegram chats used by
// the OTHER runtimes' receive loops. Under Claude Code the inbound loop lives in
// the Claude channels plugin (which keeps its own state), so this file is
// usually absent — GetTelegramTargets then falls back to the configured owner id
// (config.TelegramUserID), which is the one DM target the device provably knows,
// so proactive Broadcast/SendToUser still reach the owner.
//
// Schema: {"targets":[{"chat_id":"...","type":"private|group"}, ...]}
const telegramTargetsFile = "/root/.lumi/telegram_targets.json"

type telegramTargetEntry struct {
	ChatID string `json:"chat_id"`
	Type   string `json:"type"`
}

type telegramTargetsFileContent struct {
	Targets []telegramTargetEntry `json:"targets"`
}

// targetsFileMu serialises read-modify-write on telegramTargetsFile.
var targetsFileMu sync.Mutex

// GetTelegramBotToken returns the bot token from Device config. There is no
// agent-side config to consult under Claude Code.
func (s *ClaudeCodeService) GetTelegramBotToken() string {
	return s.config.TelegramBotToken
}

// GetTelegramTargets reads the Device-owned target store, falling back to the
// configured owner id when the store is absent/empty (the Claude channels
// plugin owns the receive loop and never populates this file — see
// telegramTargetsFile).
func (s *ClaudeCodeService) GetTelegramTargets() ([]domain.TelegramTarget, error) {
	targetsFileMu.Lock()
	data, err := os.ReadFile(telegramTargetsFile)
	targetsFileMu.Unlock()
	if err != nil {
		if os.IsNotExist(err) {
			return s.ownerTargetFallback(), nil
		}
		return nil, fmt.Errorf("read telegram_targets.json: %w", err)
	}
	var content telegramTargetsFileContent
	if err := json.Unmarshal(data, &content); err != nil {
		return nil, fmt.Errorf("parse telegram_targets.json: %w", err)
	}
	out := make([]domain.TelegramTarget, 0, len(content.Targets))
	seen := make(map[string]bool, len(content.Targets))
	for _, t := range content.Targets {
		if t.ChatID == "" || seen[t.ChatID] {
			continue
		}
		seen[t.ChatID] = true
		chatType := t.Type
		if chatType == "" {
			if strings.HasPrefix(t.ChatID, "-") {
				chatType = "group"
			} else {
				chatType = "private"
			}
		}
		out = append(out, domain.TelegramTarget{ChatID: t.ChatID, Type: chatType})
	}
	if len(out) == 0 {
		return s.ownerTargetFallback(), nil
	}
	return out, nil
}

// ownerTargetFallback returns the configured owner's DM as the single known
// target, or nil when no telegram user id is configured.
func (s *ClaudeCodeService) ownerTargetFallback() []domain.TelegramTarget {
	id := strings.TrimSpace(s.config.TelegramUserID)
	if id == "" {
		return nil
	}
	return []domain.TelegramTarget{{ChatID: id, Type: "private"}}
}

func (s *ClaudeCodeService) Broadcast(msg string, imagePath string) error {
	var sent int
	var lastErr error
	for _, ch := range s.channels {
		if !ch.IsConfigured() {
			continue
		}
		if err := ch.Send(msg, imagePath); err != nil {
			slog.Error("broadcast failed", "component", "claudecode", "channel", ch.Name(), "err", err)
			lastErr = err
			continue
		}
		sent++
	}
	if sent == 0 && lastErr != nil {
		return lastErr
	}
	if sent == 0 {
		slog.Warn("broadcast: no channels configured", "component", "claudecode")
	}
	return nil
}

func (s *ClaudeCodeService) SendToUser(telegramID string, msg string, imagePath string) error {
	if telegramID == "" {
		return nil
	}
	for _, ch := range s.channels {
		if !ch.IsConfigured() {
			continue
		}
		if sender, ok := ch.(*TelegramSender); ok {
			return sender.SendToUser(telegramID, msg, imagePath)
		}
	}
	slog.Warn("sendToUser: no telegram channel configured", "component", "claudecode")
	return nil
}

func (s *ClaudeCodeService) SendToUserWithMedia(telegramID string, msg string, imagePaths []string) error {
	if telegramID == "" {
		return nil
	}
	switch len(imagePaths) {
	case 0:
		return s.SendToUser(telegramID, msg, "")
	case 1:
		return s.SendToUser(telegramID, msg, imagePaths[0])
	}
	for _, ch := range s.channels {
		if !ch.IsConfigured() {
			continue
		}
		if sender, ok := ch.(*TelegramSender); ok {
			return sender.SendToUserWithMedia(telegramID, msg, imagePaths)
		}
	}
	slog.Warn("sendToUserWithMedia: no telegram channel configured", "component", "claudecode")
	return nil
}
