package buddy

import (
	"log"

	"claude-desktop-buddy/httpapi"
)

// ApprovalService is the Claude Desktop voice-approval use case: validate the
// pending prompt, relay the decision to Claude Desktop over BLE, and update the
// lifetime counters. It implements httpapi.ApprovalService.
//
// It depends on package-main infrastructure (the state machine and the BLE
// transport) directly — those are this daemon's concrete drivers, and the HTTP
// delivery layer is already decoupled from them via the httpapi.ApprovalService
// port, which is the boundary that matters.
type ApprovalService struct {
	sm  *StateMachine
	ble *BLEServer
}

func NewApprovalService(sm *StateMachine, ble *BLEServer) *ApprovalService {
	return &ApprovalService{sm: sm, ble: ble}
}

func (a *ApprovalService) Approve(id string) error { return a.decide(id, "once") }
func (a *ApprovalService) Deny(id string) error    { return a.decide(id, "deny") }

func (a *ApprovalService) decide(id, decision string) error {
	pending := a.sm.PendingPrompt()
	if pending == nil {
		return httpapi.ErrNoPending
	}
	if pending.ID != id {
		return httpapi.ErrPromptMismatch
	}
	if err := a.ble.Send(MakePermission(id, decision)); err != nil {
		return err
	}
	if decision == "once" {
		a.sm.Approved()
		log.Printf("[approval] approved prompt %s", id)
	} else {
		a.sm.Denied()
		log.Printf("[approval] denied prompt %s", id)
	}
	return nil
}
