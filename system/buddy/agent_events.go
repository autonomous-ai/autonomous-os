package buddy

import (
	"encoding/json"
	"regexp"

	"github.com/gorilla/websocket"
)

// AgentEvent is a bounded status notification, never an instruction or a transcript.
type AgentEvent struct {
	Type      string `json:"type"`
	ProjectID string `json:"project_id"`
	SessionID string `json:"session_id"`
	Seq       uint64 `json:"seq"`
	Status    string `json:"status"`
	Title     string `json:"title"`
	Summary   string `json:"summary"`
}

var agentIdentifier = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

// acceptAgentEvent binds events to the currently paired socket and deduplicates
// reconnect replay for this server lifetime. New server processes have no cursor.
func (s *Service) acceptAgentEvent(conn *websocket.Conn, buddyID string, data []byte) (AgentEvent, bool) {
	var event AgentEvent
	if len(data) > 16*1024 || json.Unmarshal(data, &event) != nil || event.Type != "agent_event" ||
		!agentIdentifier.MatchString(event.ProjectID) || !agentIdentifier.MatchString(event.SessionID) ||
		event.Seq == 0 || len(event.Title) > 512 || len(event.Summary) > 8192 {
		return event, false
	}
	switch event.Status {
	case "completed", "needs_input", "error":
	default:
		return event, false
	}
	s.agentMu.Lock()
	defer s.agentMu.Unlock()
	if conn == nil || s.registry.Conn() != conn {
		return event, false
	}
	if s.agentSeq == nil {
		s.agentSeq = make(map[string]uint64)
	}
	key := buddyID + "/" + event.ProjectID + "/" + event.SessionID
	if event.Seq <= s.agentSeq[key] {
		return event, false
	}
	// A paired desktop cannot grow device memory without bound. Existing sessions
	// retain their cursor; new sessions require a server restart past this limit.
	if _, exists := s.agentSeq[key]; !exists && len(s.agentSeq) >= 10000 {
		return event, false
	}
	s.agentSeq[key] = event.Seq
	return event, true
}

// RetryAgentEvent releases only this event's cursor after delivery fails. A newer
// completion must not be overwritten by a delayed failure from an older turn.
func (s *Service) RetryAgentEvent(buddyID string, event AgentEvent) {
	s.agentMu.Lock()
	defer s.agentMu.Unlock()
	key := buddyID + "/" + event.ProjectID + "/" + event.SessionID
	if s.agentSeq[key] == event.Seq {
		delete(s.agentSeq, key)
	}
}
