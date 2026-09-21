package intent

import (
	"context"
	"strings"

	"go.autonomous.ai/os/system/intent/jev"
)

// SemanticResolver recognizes a fixed action; it never executes hardware.
type SemanticResolver interface {
	Resolve(context.Context, string, []jev.Candidate, jev.Options) string
}

// MatchWithFallback keeps the local fast path intact. Only an unmatched turn
// reaches the optional resolver. Once execution is attempted its result stays
// handled, including failure, so the main agent cannot repeat a partial action.
func MatchWithFallback(ctx context.Context, text string, resolver SemanticResolver, options jev.Options) *Result {
	if ctx.Err() != nil {
		return nil
	}
	if result := Match(text); result != nil {
		return result
	}
	if resolver == nil || !options.Enabled || strings.TrimSpace(options.Endpoint) == "" || strings.TrimSpace(options.APIKey) == "" {
		return nil
	}
	candidates := semanticCandidates()
	if len(candidates) == 0 {
		return nil
	}
	id := resolver.Resolve(ctx, text, candidates, options)
	if ctx.Err() != nil {
		return nil
	}
	selected := semanticCommand(id, candidates)
	result := selected.execute()
	if result != nil {
		result.Source = "jev"
	}
	return result
}

func semanticCandidates() []jev.Candidate {
	var candidates []jev.Candidate
	for _, candidate := range jev.Candidates() {
		for _, r := range rules {
			// Legacy local rules allow an unknown body; semantic execution
			// instead requires positive evidence for each capability.
			if r.name == candidate.ID && deviceCaps[r.capability] {
				candidates = append(candidates, candidate)
				break
			}
		}
	}
	return candidates
}

func semanticCommand(id string, offered []jev.Candidate) *command {
	for _, candidate := range offered {
		if candidate.ID != id {
			continue
		}
		for i := range rules {
			r := &rules[i]
			if r.name == id && deviceCaps[r.capability] {
				return &command{rule: r}
			}
		}
	}
	return nil
}
