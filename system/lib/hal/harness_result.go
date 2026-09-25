package hal

import "encoding/json"

// SpeakHarnessReplyForTurn preserves ownership for cancellation during HAL
// synthesis while retaining the shared voice and Harness result cue.
func SpeakHarnessReplyForTurn(text, turnID string) error {
	body, _ := json.Marshal(map[string]any{"text": text, "turn_id": turnID, "realtime_feedback": true, "harness_result": true})
	return postSpeak("/voice/speak", body)
}
