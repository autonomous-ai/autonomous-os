package domain

import "strings"

// IsExternallyOwnedRuntime is the ownership boundary for the text-only Welcome
// Desk. Selecting it grants no installer, service, HAL, channel or config rights.
func IsExternallyOwnedRuntime(runtime string) bool {
	return strings.EqualFold(strings.TrimSpace(runtime), AgentRuntimeIntern)
}

func IsTextOnlyGateway(g AgentGateway) bool {
	return g != nil && IsExternallyOwnedRuntime(g.Name())
}
