package httpapi

import (
	"encoding/json"
	"net/http"
)

// NotifyRequest / UsageRequest are the Claude Code Buddy plugin push payloads
// (POST /claude-code/notify and POST /claude-code/usage).
type NotifyRequest struct {
	Title    string `json:"title"`
	Subtitle string `json:"subtitle"`
	Level    string `json:"level"`
	Sound    bool   `json:"sound"`
}

type UsageRequest struct {
	FiveHour int    `json:"five_hour"`
	SevenDay int    `json:"seven_day"`
	Reset5h  string `json:"reset_5h"`
	Reset7d  string `json:"reset_7d"`
	Sound    bool   `json:"sound"`
}

func (s *Server) handleNotify(w http.ResponseWriter, r *http.Request) {
	var req NotifyRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json")
		return
	}
	s.activity.Notify(req.Level, req.Title, req.Subtitle, req.Sound)
	ok(w)
}

func (s *Server) handleUsage(w http.ResponseWriter, r *http.Request) {
	var req UsageRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json")
		return
	}
	s.activity.Usage(req.FiveHour, req.SevenDay, req.Reset5h, req.Reset7d, req.Sound)
	ok(w)
}
