package main

import (
	"crypto/rand"
	"encoding/hex"
	"strings"
	"time"
)

// Command matches the JSON shape the buddy expects on its WebSocket.
// Identical to what lumi will use in `lumi/internal/buddy/types.go`.
type Command struct {
	ID        string         `json:"id"`
	Action    string         `json:"action"`
	Params    map[string]any `json:"params"`
	TimeoutMs int            `json:"timeout_ms,omitempty"`
	IssuedAt  string         `json:"issued_at,omitempty"`
	IssuedBy  string         `json:"issued_by,omitempty"`
}

func newCommand(action string, params map[string]any) Command {
	idBytes := make([]byte, 8)
	_, _ = rand.Read(idBytes)
	return Command{
		ID:        hex.EncodeToString(idBytes),
		Action:    action,
		Params:    params,
		TimeoutMs: 5000,
		IssuedAt:  time.Now().UTC().Format(time.RFC3339),
		IssuedBy:  "mock-lamp:repl",
	}
}

// parseREPL turns a one-line REPL input into a Command, or (zero, false) if unrecognized.
func parseREPL(line string) (Command, bool) {
	line = strings.TrimSpace(line)
	if line == "" {
		return Command{}, false
	}
	parts := strings.SplitN(line, " ", 2)
	action := parts[0]
	rest := ""
	if len(parts) == 2 {
		rest = strings.TrimSpace(parts[1])
	}

	switch action {
	case "ping":
		return newCommand("ping", map[string]any{}), true
	case "open_app":
		return newCommand("open_app", map[string]any{"app": fallback(rest, "Calculator")}), true
	case "close_app":
		return newCommand("close_app", map[string]any{"app": fallback(rest, "Calculator")}), true
	case "open_url":
		return newCommand("open_url", map[string]any{"url": fallback(rest, "https://example.com")}), true
	case "type_text":
		return newCommand("type_text", map[string]any{"text": fallback(rest, "hello from lumi")}), true
	case "key_combo":
		if rest == "" {
			return Command{}, false
		}
		return newCommand("key_combo", map[string]any{"keys": strings.Fields(rest)}), true
	case "notification":
		return newCommand("notification", map[string]any{
			"title": fallback(rest, "Lumi"),
			"body":  "Test from mock-lamp",
		}), true
	}
	return Command{}, false
}

func fallback(s, def string) string {
	if s == "" {
		return def
	}
	return s
}
