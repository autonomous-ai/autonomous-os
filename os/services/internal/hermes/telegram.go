package hermes

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"go.autonomous.ai/os/domain"
)

// telegramTargetsFile is the Device-owned store of known Telegram chats. Hermes
// has no plugin/channel layer of its own (unlike OpenClaw which keeps
// agents/main/sessions/sessions.json), so the receive loop populates this
// file each time a new chat DMs the bot.
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

// targetsFileMu serialises read-modify-write on telegramTargetsFile. Held only
// across the disk I/O so the receive loop never blocks the broadcast path.
var targetsFileMu sync.Mutex

// GetTelegramBotToken returns the bot token from Device config. There is no
// agent-side config to consult under Hermes.
func (s *HermesService) GetTelegramBotToken() string {
	return s.config.TelegramBotToken
}

// GetTelegramTargets reads the Device-owned target store. Returns nil + nil
// (no error) when the file doesn't exist yet — that's the steady state before
// any user has messaged the bot.
func (s *HermesService) GetTelegramTargets() ([]domain.TelegramTarget, error) {
	targetsFileMu.Lock()
	data, err := os.ReadFile(telegramTargetsFile)
	targetsFileMu.Unlock()
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
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
	return out, nil
}

// upsertTelegramTarget appends chatID to the store if not present. Called by
// the receive loop (when it lands) so future broadcasts reach this user.
func upsertTelegramTarget(chatID, chatType string) error {
	if chatID == "" {
		return nil
	}
	if chatType == "" {
		if strings.HasPrefix(chatID, "-") {
			chatType = "group"
		} else {
			chatType = "private"
		}
	}
	targetsFileMu.Lock()
	defer targetsFileMu.Unlock()

	var content telegramTargetsFileContent
	data, err := os.ReadFile(telegramTargetsFile)
	if err == nil {
		_ = json.Unmarshal(data, &content)
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("read telegram_targets.json: %w", err)
	}
	for _, t := range content.Targets {
		if t.ChatID == chatID {
			return nil
		}
	}
	content.Targets = append(content.Targets, telegramTargetEntry{ChatID: chatID, Type: chatType})

	if err := os.MkdirAll(filepath.Dir(telegramTargetsFile), 0o755); err != nil {
		return fmt.Errorf("mkdir telegram store: %w", err)
	}
	out, err := json.MarshalIndent(content, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal telegram_targets: %w", err)
	}
	if err := os.WriteFile(telegramTargetsFile, out, 0o600); err != nil {
		return fmt.Errorf("write telegram_targets.json: %w", err)
	}
	return nil
}

func (s *HermesService) Broadcast(msg string, imagePath string) error {
	var sent int
	var lastErr error
	for _, ch := range s.channels {
		if !ch.IsConfigured() {
			continue
		}
		if err := ch.Send(msg, imagePath); err != nil {
			slog.Error("broadcast failed", "component", "hermes", "channel", ch.Name(), "err", err)
			lastErr = err
			continue
		}
		sent++
	}
	if sent == 0 && lastErr != nil {
		return lastErr
	}
	if sent == 0 {
		slog.Warn("broadcast: no channels configured", "component", "hermes")
	}
	return nil
}

func (s *HermesService) SendToUser(telegramID string, msg string, imagePath string) error {
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
	slog.Warn("sendToUser: no telegram channel configured", "component", "hermes")
	return nil
}

func (s *HermesService) SendToUserWithMedia(telegramID string, msg string, imagePaths []string) error {
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
	slog.Warn("sendToUserWithMedia: no telegram channel configured", "component", "hermes")
	return nil
}
