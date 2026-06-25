package httpapi

import (
	"net/http"
	"time"
)

// StatusResponse is the JSON shape of GET /status.
type StatusResponse struct {
	State           string        `json:"state"`
	Connected       bool          `json:"connected"`
	SessionsRunning int           `json:"sessions_running"`
	TokensToday     int           `json:"tokens_today"`
	PendingPrompt   *PromptDetail `json:"pending_prompt"`
}

type PromptDetail struct {
	ID         string `json:"id"`
	Tool       string `json:"tool"`
	Hint       string `json:"hint"`
	ReceivedAt string `json:"received_at"`
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	st := s.status.Status()
	resp := StatusResponse{
		State:           st.State,
		Connected:       st.Connected,
		SessionsRunning: st.SessionsRunning,
		TokensToday:     st.TokensToday,
	}
	if st.HasPending {
		resp.PendingPrompt = &PromptDetail{
			ID:   st.PendingID,
			Tool: st.PendingTool,
			Hint: st.PendingHint,
		}
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status":          "ok",
		"ble_advertising": true,
		"uptime_seconds":  int(time.Since(s.startAt).Seconds()),
	})
}
