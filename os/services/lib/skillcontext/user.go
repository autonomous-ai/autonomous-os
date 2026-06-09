package skillcontext

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"

	"go.autonomous.ai/os/lib/hal"
	"go.autonomous.ai/os/lib/usercanon"
)

const userInfoTimeout = 600 * time.Millisecond

// userInfo mirrors hal's GET /user/info?name=... payload.
// Schema (verified on Pi): {name, is_friend, telegram_id, telegram_username}.
type userInfo struct {
	Name             string `json:"name"`
	IsFriend         bool   `json:"is_friend"`
	TelegramID       string `json:"telegram_id,omitempty"`
	TelegramUsername string `json:"telegram_username,omitempty"`
}

// BuildUserContext returns a `[user_info: {...}]` block so SKILLs do not have
// to issue `curl /user/info?name={user}` for telegram_id / known-user routing.
// Returns "" on hard failure or when user is empty/unknown so the agent can
// still fall back to the original fetch.
func BuildUserContext(user string) string {
	user = usercanon.Resolve(user)
	if user == "" || user == "unknown" {
		return ""
	}
	client := &http.Client{Timeout: userInfoTimeout}
	resp, err := client.Get(hal.BaseURL + "/user/info?name=" + user)
	if err != nil {
		slog.Warn("user context: fetch failed", "component", "skillcontext", "user", user, "error", err)
		return ""
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return ""
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if err != nil {
		return ""
	}
	var info userInfo
	if json.Unmarshal(body, &info) != nil {
		return ""
	}
	out, err := json.Marshal(info)
	if err != nil {
		return ""
	}
	return fmt.Sprintf("\n[user_info: %s]", string(out))
}
