package httpapi

import (
	"errors"
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
	if err := decodeJSON(w, r, &req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json")
		return
	}
	s.activity.Notify(req.Level, req.Title, req.Subtitle, req.Sound)
	ok(w)
}

func (s *Server) handleUsage(w http.ResponseWriter, r *http.Request) {
	var req UsageRequest
	if err := decodeJSON(w, r, &req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json")
		return
	}
	s.activity.Usage(req.FiveHour, req.SevenDay, req.Reset5h, req.Reset7d, req.Sound)
	ok(w)
}

// --- Reverse approval (device → Claude Code) ---

// CodeApprovalRequestBody is the body of POST /claude-code/approval-request, sent
// by the plugin's permission hook on the Mac. The hook BLOCKS on this request
// until the on-device agent resolves it (or it times out).
type CodeApprovalRequestBody struct {
	ID    string         `json:"id"`
	Tool  string         `json:"tool"`
	Hint  string         `json:"hint"`
	Input map[string]any `json:"input"`
}

// handleApprovalRequest long-polls: it blocks until the device agent approves /
// denies the prompt or the use case times out, then returns the decision. The
// hook maps "allow"/"deny" to a PermissionRequest decision and anything else
// (incl. "timeout") to "defer to the native dialog".
func (s *Server) handleApprovalRequest(w http.ResponseWriter, r *http.Request) {
	var req CodeApprovalRequestBody
	if err := decodeJSON(w, r, &req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json")
		return
	}
	if req.ID == "" {
		fail(w, http.StatusBadRequest, "missing id")
		return
	}
	if req.Hint == "" {
		req.Hint = deriveHint(req.Tool, req.Input)
	}
	decision, _ := s.codeApprovals.Request(r.Context(), CodeApprovalRequest{
		ID:    req.ID,
		Tool:  req.Tool,
		Hint:  req.Hint,
		Input: req.Input,
	})
	if decision == "" {
		decision = "timeout"
	}
	writeJSON(w, http.StatusOK, map[string]any{"decision": decision})
}

// handleCodeApprove / handleCodeDeny are called by the on-device agent (the
// OpenClaw skill) after the user answers by voice. They are bound to loopback —
// only the device itself should be able to let code run on the user's Mac.
func (s *Server) handleCodeApprove(w http.ResponseWriter, r *http.Request) {
	s.codeDecide(w, r, s.codeApprovals.Approve)
}

func (s *Server) handleCodeDeny(w http.ResponseWriter, r *http.Request) {
	s.codeDecide(w, r, s.codeApprovals.Deny)
}

func (s *Server) codeDecide(w http.ResponseWriter, r *http.Request, action func(id string) error) {
	if !isLoopback(r) {
		fail(w, http.StatusForbidden, "approve/deny is loopback-only")
		return
	}
	var req ApprovalRequest // reuse {id}
	if err := decodeJSON(w, r, &req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json")
		return
	}
	switch err := action(req.ID); {
	case errors.Is(err, ErrNoPending):
		fail(w, http.StatusConflict, err.Error())
	case err != nil:
		fail(w, http.StatusInternalServerError, "resolve failed")
	default:
		ok(w)
	}
}

// handleCodePending lists currently-blocked code approvals so the on-device
// agent (or a debugging human) can see what's awaiting a voice answer.
func (s *Server) handleCodePending(w http.ResponseWriter, r *http.Request) {
	pending := s.codeApprovals.Pending()
	out := make([]map[string]any, 0, len(pending))
	for _, p := range pending {
		out = append(out, map[string]any{"id": p.ID, "tool": p.Tool, "hint": p.Hint})
	}
	writeJSON(w, http.StatusOK, map[string]any{"pending": out})
}

// deriveHint produces a short human description of a tool call for TTS / display
// when the hook didn't supply one.
func deriveHint(tool string, input map[string]any) string {
	if input != nil {
		for _, key := range []string{"command", "file_path", "path", "url", "pattern", "query"} {
			if v, ok := input[key].(string); ok && v != "" {
				return v
			}
		}
	}
	return tool
}
