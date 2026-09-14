package externalhistory

import (
	"crypto/rand"
	"fmt"
	"strings"
)

// RecordRealtime adapts HAL's existing handled-turn notification to the shared
// journal. The HTTP adapter keeps transport-only snapshot markers in flow logs.
func (s *Store) RecordRealtime(origin, message string) (string, error) {
	message = strings.TrimPrefix(message, "[skills: input-branching]\n")
	if !strings.HasPrefix(message, "[HANDLED] ") {
		return "", fmt.Errorf("realtime history is missing HANDLED input")
	}
	input, output, ok := strings.Cut(strings.TrimPrefix(message, "[HANDLED] "), "\n[REPLY] ")
	if !ok {
		return "", fmt.Errorf("realtime history is missing REPLY output")
	}
	if origin == "" {
		origin = rand.Text()
	}
	r, err := s.RecordCompleted(Record{
		Source: "realtime", OriginRunID: origin, AgentName: "Realtime voice",
		Input: input, Output: output,
	})
	return r.SyncRunID, err
}
