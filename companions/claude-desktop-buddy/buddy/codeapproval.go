package buddy

import (
	"context"
	"log"
	"sync"
	"time"

	"claude-desktop-buddy/httpapi"
)

// CodeApprovals implements httpapi.CodeApprovalService: the device side of the
// Claude Code reverse-approval round-trip.
//
// The Mac plugin's permission hook calls Request, which registers a pending
// approval, fires the device cue (voice + LED + sensing event so the on-device
// agent asks the user), then blocks on a per-request channel until the agent
// POSTs /claude-code/approve|deny (-> Approve/Deny) or the request times out.
//
// Keyed by id, so multiple concurrent prompts (parallel tool calls / multiple
// Claude Code sessions) each block independently.
type CodeApprovals struct {
	mu     sync.Mutex
	items  map[string]*codeEntry
	bridge *Bridge
	ttl    time.Duration
}

type codeEntry struct {
	req     httpapi.CodeApprovalRequest
	ch      chan string // buffered(1); first writer wins, later resolves are no-ops
	created time.Time
}

// NewCodeApprovals builds the service. ttl bounds how long a single Request
// blocks before falling back to "timeout" (the hook then defers to Claude Code's
// native dialog).
func NewCodeApprovals(bridge *Bridge, ttl time.Duration) *CodeApprovals {
	if ttl <= 0 {
		ttl = 55 * time.Second
	}
	return &CodeApprovals{
		items:  make(map[string]*codeEntry),
		bridge: bridge,
		ttl:    ttl,
	}
}

func (c *CodeApprovals) Request(ctx context.Context, req httpapi.CodeApprovalRequest) (string, error) {
	if req.ID == "" {
		return "timeout", nil
	}
	e := &codeEntry{req: req, ch: make(chan string, 1), created: time.Now()}

	c.mu.Lock()
	c.items[req.ID] = e
	c.mu.Unlock()
	defer func() {
		c.mu.Lock()
		delete(c.items, req.ID)
		c.mu.Unlock()
	}()

	log.Printf("[code-approval] pending %s tool=%s hint=%q", req.ID, req.Tool, req.Hint)
	// Announce/restore make blocking HAL+OS-server HTTP calls (5s timeout each).
	// Run them off the request goroutine so a slow/hung device never delays the
	// long-poll from starting (which would eat into the user's answer window).
	go c.bridge.announceCodeApproval(req)
	defer func() { go c.bridge.restoreAfterCodeApproval() }()

	select {
	case d := <-e.ch:
		log.Printf("[code-approval] %s resolved: %s", req.ID, d)
		return d, nil
	case <-time.After(c.ttl):
		log.Printf("[code-approval] %s timed out after %s", req.ID, c.ttl)
		return "timeout", nil
	case <-ctx.Done():
		// Hook gave up / client disconnected — treat as no decision.
		log.Printf("[code-approval] %s cancelled: %v", req.ID, ctx.Err())
		return "timeout", ctx.Err()
	}
}

func (c *CodeApprovals) Approve(id string) error { return c.resolve(id, "allow") }
func (c *CodeApprovals) Deny(id string) error    { return c.resolve(id, "deny") }

func (c *CodeApprovals) resolve(id, decision string) error {
	c.mu.Lock()
	e := c.items[id]
	c.mu.Unlock()
	if e == nil {
		return httpapi.ErrNoPending
	}
	// Non-blocking send: the channel is buffered(1), so a second resolve for the
	// same id is a silent no-op (first decision wins) instead of blocking.
	select {
	case e.ch <- decision:
	default:
	}
	return nil
}

func (c *CodeApprovals) Pending() []httpapi.CodeApprovalRequest {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]httpapi.CodeApprovalRequest, 0, len(c.items))
	for _, e := range c.items {
		out = append(out, e.req)
	}
	return out
}
