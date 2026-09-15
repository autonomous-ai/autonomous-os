package device

import (
	"strings"

	"go.autonomous.ai/os/system/domain"
)

// SelectExternalRuntime selects across the externally-owned boundary without
// installation, readiness claims, service control or HAL/config migration.
// Both entering and leaving require an operator-managed os-server restart.
func (s *Service) SelectExternalRuntime(d domain.AgentRuntimeSetData) error {
	if !s.runtimeSwitchMu.TryLock() {
		return ErrAgentRuntimeSwitchInProgress
	}
	defer s.runtimeSwitchMu.Unlock()
	target := strings.ToLower(strings.TrimSpace(d.Runtime))
	if s.config == nil || !domain.IsValidAgentRuntime(target) || target == domain.AgentRuntimeRemote || d.URL != "" || d.Token != "" {
		return domain.ErrNotSupportedByRuntime
	}
	if !domain.IsExternallyOwnedRuntime(target) && !domain.IsExternallyOwnedRuntime(s.config.AgentRuntimeValue()) && !domain.IsTextOnlyGateway(s.agentGateway) {
		return domain.ErrNotSupportedByRuntime
	}
	return s.config.SelectAgentRuntime(target)
}

// externalRuntime gates effects on the instantiated gateway, never the saved
// selection. Selecting Intern must leave the active device runtime intact until
// restart. Selection-boundary checks belong in SelectExternalRuntime and switching.
func (s *Service) externalRuntime() bool {
	// A pending selection cannot change the ownership of the active service.
	return domain.IsTextOnlyGateway(s.agentGateway)
}
