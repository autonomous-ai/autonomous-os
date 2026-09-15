package server

import "go.autonomous.ai/os/system/device"

// activeAgentRuntime follows the instantiated gateway. Config is a fallback
// only for callers without a gateway (for example, before initialization).
func (s *Server) activeAgentRuntime() string {
	if s.agentGateway != nil {
		return s.agentGateway.Name()
	}
	return device.CurrentAgentRuntimeFromConfig(s.config)
}
