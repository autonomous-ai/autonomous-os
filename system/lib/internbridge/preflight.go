package internbridge

import "context"

// ProbeGeneration exercises the generation path with a fixed public prompt.
// Unlike Ready, it requires a valid draft response; fallback and routing-only
// outcomes are errors. It neither caches readiness nor reports process uptime.
// A successful probe proves only this bridge's generation response at this
// instant, not provider identity, model locality, or AgentGateway readiness.
// The pinned bridge's audited LocalModel implementation enforces local routing;
// protocol 0.2.0 provides no authentication or provider attestation.
//
// This is an explicit inference request (up to 256 generated tokens with the
// pinned bridge), not a cheap liveness check. Callers control its frequency.
// No user text, history, device data, or caller-supplied classification is used.
// No run ID is reused: the bridge reserves supplied IDs without result lookup.
func (c *Client) ProbeGeneration(ctx context.Context) error {
	if ctx == nil {
		return failure(ErrInvalidRequest, 0)
	}
	// One deadline bounds both HTTP calls, rather than one timeout per stage.
	ctx, cancel := context.WithTimeout(ctx, RequestTimeout)
	defer cancel()
	if err := c.Ready(ctx); err != nil {
		return err
	}
	_, err := c.Do(ctx, Request{
		Text:      "Write a brief greeting.",
		Operation: Generate,
		DataClass: Public,
	})
	return err
}
