package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
)

// Claude Desktop path: the OpenClaw agent on the device approves or denies a
// pending Claude Desktop permission prompt; the decision is relayed back to
// Claude Desktop over BLE by the ApprovalService implementation.

// ApprovalRequest is the body of POST /claude-desktop/approve and /deny.
type ApprovalRequest struct {
	ID string `json:"id"`
}

func (s *Server) handleApprove(w http.ResponseWriter, r *http.Request) {
	s.decide(w, r, s.approvals.Approve)
}

func (s *Server) handleDeny(w http.ResponseWriter, r *http.Request) {
	s.decide(w, r, s.approvals.Deny)
}

// decide is the shared approve/deny flow: decode, run the use case, then map the
// outcome to an HTTP status.
func (s *Server) decide(w http.ResponseWriter, r *http.Request, action func(id string) error) {
	var req ApprovalRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json")
		return
	}
	switch err := action(req.ID); {
	case errors.Is(err, ErrNoPending), errors.Is(err, ErrPromptMismatch):
		fail(w, http.StatusConflict, err.Error())
	case err != nil:
		fail(w, http.StatusInternalServerError, "ble send failed")
	default:
		ok(w)
	}
}
