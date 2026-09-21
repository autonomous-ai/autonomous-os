package jev

import (
	"context"
	"log/slog"
	"strings"
	"sync/atomic"
	"time"
)

const (
	DefaultTimeout   = 350 * time.Millisecond
	MaxTimeout       = time.Second
	jevErrorCooldown = 30 * time.Second
	jevMaxInputBytes = 2000
)

// Options are explicit per-request settings. Disabled is the zero value.
// Endpoint and APIKey come from config.json's Autonomous proxy settings.
type Options struct {
	Enabled  bool
	Endpoint string
	APIKey   string
	Timeout  time.Duration
}

type jevDecider interface {
	decide(context.Context, string, string, string, []Candidate) (string, error)
}

// Resolver classifies a request without performing any hardware action.
// Reuse one per handler: concurrent calls skip rather than queue, and provider
// errors open a brief cooldown. No hardware work runs in a detached goroutine.
type Resolver struct {
	client     jevDecider
	busy       atomic.Bool
	retryAfter atomic.Int64
}

func NewResolver() *Resolver { return &Resolver{client: &jevClient{}} }

// Resolve returns a code-owned candidate ID or an empty string to defer to the
// main agent. The caller owns capability checks and all hardware execution.
func (r *Resolver) Resolve(ctx context.Context, text string, candidates []Candidate, options Options) string {
	if ctx.Err() != nil {
		return ""
	}
	if r == nil || !options.Enabled || strings.TrimSpace(options.Endpoint) == "" || strings.TrimSpace(options.APIKey) == "" {
		return ""
	}
	text = jevText(text)
	if text == "" || len(text) > jevMaxInputBytes {
		return ""
	}
	if len(candidates) == 0 || time.Now().UnixNano() < r.retryAfter.Load() || !r.busy.CompareAndSwap(false, true) {
		return ""
	}
	defer r.busy.Store(false)
	budget := options.Timeout
	if budget <= 0 {
		budget = DefaultTimeout
	} else if budget > MaxTimeout {
		budget = MaxTimeout
	}
	deadline, cancel := context.WithTimeout(ctx, budget)
	defer cancel()
	started := time.Now()
	id, err := r.client.decide(deadline, options.Endpoint, options.APIKey, text, candidates)
	elapsed := time.Since(started).Milliseconds()
	outcome := "abstain"
	defer func() {
		// Never log credentials, provider bodies or the utterance here.
		slog.Info("intent Jev decision", "component", "intent", "outcome", outcome,
			"decision_ms", elapsed)
	}()
	if err != nil || deadline.Err() != nil {
		outcome = "error"
		if ctx.Err() == nil {
			r.retryAfter.Store(time.Now().Add(jevErrorCooldown).UnixNano())
		}
		return ""
	}
	for _, candidate := range candidates {
		if id == candidate.ID {
			outcome = "selected"
			return id
		}
	}
	return ""
}
