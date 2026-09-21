package jev

import (
	"context"
	"errors"
	"testing"
	"time"
)

type fakeJevDecider func(context.Context, string, string, []Candidate) (string, error)

func (f fakeJevDecider) decide(ctx context.Context, endpoint, key, text string, candidates []Candidate) (string, error) {
	return f(ctx, key, text, candidates)
}

func TestJevDeadlineAndErrorsFallThroughWithoutLateExecution(t *testing.T) {
	for _, late := range []bool{false, true} {
		providerCalls := 0
		r := &Resolver{client: fakeJevDecider(func(ctx context.Context, _ string, _ string, _ []Candidate) (string, error) {
			providerCalls++
			if late {
				<-ctx.Done()
				return "dim", nil // Even a late success cannot execute hardware.
			}
			return "", errors.New("provider unavailable")
		})}
		opts := Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test", Timeout: 5 * time.Millisecond}
		start := time.Now()
		if r.Resolve(context.Background(), "ánh sáng chói quá", Candidates(), opts) != "" {
			t.Fatal("failed or late decision executed")
		}
		if time.Since(start) > time.Second {
			t.Fatal("decision budget was ignored")
		}
		if r.Resolve(context.Background(), "ánh sáng chói quá", Candidates(), opts) != "" || providerCalls != 1 {
			t.Fatal("error cooldown retried provider")
		}

	}
}

func TestJevConcurrentMissSkipsInsteadOfWaiting(t *testing.T) {
	entered, release := make(chan struct{}), make(chan struct{})
	r := &Resolver{client: fakeJevDecider(func(context.Context, string, string, []Candidate) (string, error) {
		close(entered)
		<-release
		return "", nil
	})}
	opts := Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"}
	done := make(chan struct{})
	go func() {
		defer close(done)
		r.Resolve(context.Background(), "ánh sáng chói quá", Candidates(), opts)
	}()
	<-entered
	got := r.Resolve(context.Background(), "ánh sáng chói quá", Candidates(), opts)
	close(release)
	<-done
	if got != "" {
		t.Fatal("busy request should fall through")
	}
}

func TestJevBoundsAndCancelledInputNeverContactProvider(t *testing.T) {
	r := &Resolver{client: fakeJevDecider(func(context.Context, string, string, []Candidate) (string, error) {
		t.Fatal("invalid input reached provider")
		return "", nil
	})}
	opts := Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"}
	for _, text := range []string{"", string(make([]byte, jevMaxInputBytes+1))} {
		if r.Resolve(context.Background(), text, Candidates(), opts) != "" {
			t.Fatal("unexpected result")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if r.Resolve(ctx, "ánh sáng chói quá", Candidates(), opts) != "" {
		t.Fatal("cancelled input executed")
	}

}

func TestJevPreservesInstructionNegationAndDoesNotMixTranscript(t *testing.T) {
	text := "[voice-instruction] Don't change my light; check my email.\n[transcript] light off [snapshot: /private/frame.jpg]"
	if got := jevText(text); got != "Don't change my light; check my email." {
		t.Fatalf("got %q", got)
	}
	for _, utterance := range []string{"[đừng] giảm sáng", "[when I get home] giảm sáng", "giảm sáng (but only tomorrow)"} {
		wrapped := "[user] [ambient] Unknown Speaker: [voice:test] " + utterance + " (audio saved at /private/test.wav)"
		if got := jevText(wrapped); got != utterance {
			t.Fatalf("lost qualifier: %q -> %q", utterance, got)
		}
	}
}
