package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"
)

// HTTPServer exposes buddy status and approval endpoints for OpenClaw skill.
type HTTPServer struct {
	port    int
	sm      *StateMachine
	ble     *BLEServer
	startAt time.Time
}

func NewHTTPServer(port int, sm *StateMachine, ble *BLEServer) *HTTPServer {
	return &HTTPServer{
		port:    port,
		sm:      sm,
		ble:     ble,
		startAt: time.Now(),
	}
}

// StatusResponse is returned by GET /status.
type StatusResponse struct {
	State           string         `json:"state"`
	Connected       bool           `json:"connected"`
	SessionsRunning int            `json:"sessions_running"`
	TokensToday     int            `json:"tokens_today"`
	PendingPrompt   *PromptDetail  `json:"pending_prompt"`
}

type PromptDetail struct {
	ID         string `json:"id"`
	Tool       string `json:"tool"`
	Hint       string `json:"hint"`
	ReceivedAt string `json:"received_at"`
}

type ApprovalRequest struct {
	ID string `json:"id"`
}

// Start begins serving HTTP on the configured port.
func (h *HTTPServer) Start() error {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /status", h.handleStatus)
	mux.HandleFunc("GET /health", h.handleHealth)
	mux.HandleFunc("POST /approve", h.handleApprove)
	mux.HandleFunc("POST /deny", h.handleDeny)

	addr := fmt.Sprintf(":%d", h.port)
	log.Printf("[http] listening on %s", addr)
	return http.ListenAndServe(addr, mux)
}

func (h *HTTPServer) handleStatus(w http.ResponseWriter, r *http.Request) {
	hb := h.sm.LastHeartbeat()

	resp := StatusResponse{
		State:     string(h.sm.State()),
		Connected: h.sm.Connected(),
	}

	if hb != nil {
		resp.SessionsRunning = hb.Running
		resp.TokensToday = hb.TokensToday
	}

	if p := h.sm.PendingPrompt(); p != nil {
		resp.PendingPrompt = &PromptDetail{
			ID:   p.ID,
			Tool: p.Tool,
			Hint: p.Hint,
		}
	}

	writeJSON(w, http.StatusOK, resp)
}

func (h *HTTPServer) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status":          "ok",
		"ble_advertising": true,
		"uptime_seconds":  int(time.Since(h.startAt).Seconds()),
	})
}

func (h *HTTPServer) handleApprove(w http.ResponseWriter, r *http.Request) {
	var req ApprovalRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{"ok": false, "error": "invalid json"})
		return
	}

	pending := h.sm.PendingPrompt()
	if pending == nil {
		writeJSON(w, http.StatusConflict, map[string]interface{}{"ok": false, "error": "no pending prompt"})
		return
	}
	if pending.ID != req.ID {
		writeJSON(w, http.StatusConflict, map[string]interface{}{"ok": false, "error": "prompt id mismatch"})
		return
	}

	data := MakePermission(req.ID, "once")
	if err := h.ble.Send(data); err != nil {
		log.Printf("[http] BLE send approve error: %v", err)
		writeJSON(w, http.StatusInternalServerError, map[string]interface{}{"ok": false, "error": "ble send failed"})
		return
	}

	h.sm.Approved()
	log.Printf("[http] approved prompt %s", req.ID)
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func (h *HTTPServer) handleDeny(w http.ResponseWriter, r *http.Request) {
	var req ApprovalRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{"ok": false, "error": "invalid json"})
		return
	}

	pending := h.sm.PendingPrompt()
	if pending == nil {
		writeJSON(w, http.StatusConflict, map[string]interface{}{"ok": false, "error": "no pending prompt"})
		return
	}
	if pending.ID != req.ID {
		writeJSON(w, http.StatusConflict, map[string]interface{}{"ok": false, "error": "prompt id mismatch"})
		return
	}

	data := MakePermission(req.ID, "deny")
	if err := h.ble.Send(data); err != nil {
		log.Printf("[http] BLE send deny error: %v", err)
		writeJSON(w, http.StatusInternalServerError, map[string]interface{}{"ok": false, "error": "ble send failed"})
		return
	}

	h.sm.Denied()
	log.Printf("[http] denied prompt %s", req.ID)
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}
