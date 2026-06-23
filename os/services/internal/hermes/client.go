package hermes

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"

	"go.autonomous.ai/os/domain"
)

// hermesSessionHeader is the response header Hermes uses to publish the
// server-side session UUID (see docs/hermes.md §3). One UUID per conversation;
// stays stable across reconnects.
const hermesSessionHeader = "X-Hermes-Session-Id"

// streamRequest is the on-wire POST body to /v1/responses. Fields are
// pointer-typed (or omitempty) so we never accidentally serialise an
// unconfigured value (Hermes is strict about input shape).
type streamRequest struct {
	Model        string `json:"model"`
	Conversation string `json:"conversation,omitempty"`
	Stream       bool   `json:"stream"`
	Instructions string `json:"instructions,omitempty"`
	Input        any    `json:"input"`
	Title        string `json:"title,omitempty"`
}

// inputContent represents one element of the multi-part input array used for
// vision turns. Plain text turns can pass Input: "<string>" instead and skip
// this entirely — Hermes accepts both shapes.
type inputContent struct {
	Type     string `json:"type"`                // "input_text" | "input_image"
	Text     string `json:"text,omitempty"`      // when Type == "input_text"
	ImageURL string `json:"image_url,omitempty"` // when Type == "input_image"; data: URL or remote URL
}

type inputMessage struct {
	Role    string         `json:"role"`
	Content []inputContent `json:"content"`
}

// streamResult is what the SSE consumer hands back once response.completed
// arrives: the response.id (for caching as last_response_id), full assistant
// text (caller may want for sync send-and-wait paths), and any reported
// session UUID.
type streamResult struct {
	// DeviceRunID is the device-side idempotency key (device-chat-N-…) the turn
	// was started with. The translator emits THIS as the runId on every WSEvent
	// (lifecycle/tool/assistant/chat) instead of Hermes's own response.id, so the
	// web monitor — which correlates replies by the runId returned from its POST —
	// matches them, exactly like OpenClaw 5.4+ echoing the idempotencyKey. Without
	// it, events carry resp_… and the web never renders the hermes reply.
	DeviceRunID string
	ResponseID  string
	SessionID   string
	FinalText   string
	Errored     bool
	ErrorText   string
}

// postStream issues POST /v1/responses with stream:true and reads the SSE
// stream until response.completed | response.failed | context cancel | EOF.
// Translated domain.WSEvent frames are dispatched via dispatch() one by one.
//
// The HTTP request is built with NO client-side timeout (the client's
// Timeout is 0); ctx is the only cancellation handle. Long agent turns are
// expected (minutes is normal), so a fixed timeout would cut them short.
func (s *Service) postStream(ctx context.Context, deviceRunID string, body streamRequest, dispatch func(domain.WSEvent)) (streamResult, error) {
	bodyBytes, err := json.Marshal(body)
	if err != nil {
		return streamResult{}, fmt.Errorf("marshal request: %w", err)
	}

	url := strings.TrimRight(BaseURL, "/") + "/v1/responses"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(bodyBytes))
	if err != nil {
		return streamResult{}, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")
	if APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+APIKey)
	}

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return streamResult{}, fmt.Errorf("do request: %w", err)
	}
	defer resp.Body.Close()

	// Capture the session UUID before reading the stream so even an immediate
	// non-200 response still updates the in-memory key for monitor display.
	if sid := resp.Header.Get(hermesSessionHeader); sid != "" {
		s.sessionUUID.Store(sid)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		raw, _ := io.ReadAll(resp.Body)
		return streamResult{}, fmt.Errorf("hermes /v1/responses status %d: %s", resp.StatusCode, truncRunes(string(raw), 400))
	}

	return s.readSSE(ctx, deviceRunID, resp.Body, dispatch)
}

// readSSE consumes the SSE byte stream line-by-line into (event, data) pairs
// and forwards each to translateAndDispatch. Buffer is sized for the largest
// tool output a single function_call_output frame might carry (8MB).
func (s *Service) readSSE(ctx context.Context, deviceRunID string, body io.Reader, dispatch func(domain.WSEvent)) (streamResult, error) {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 1<<20), 8<<20)

	var (
		currentEvent string
		dataBuf      strings.Builder
		result       = streamResult{DeviceRunID: deviceRunID}
	)

	flush := func() {
		defer func() {
			currentEvent = ""
			dataBuf.Reset()
		}()
		data := dataBuf.String()
		if data == "" {
			return
		}
		if data == "[DONE]" {
			return
		}
		s.translateSSE(currentEvent, data, dispatch, &result)
	}

	for scanner.Scan() {
		select {
		case <-ctx.Done():
			return result, ctx.Err()
		default:
		}
		line := scanner.Text()
		// Blank line terminates an event block per SSE spec.
		if line == "" {
			flush()
			continue
		}
		// Comment lines per SSE spec (used by some servers for keepalive).
		if strings.HasPrefix(line, ":") {
			continue
		}
		switch {
		case strings.HasPrefix(line, "event:"):
			currentEvent = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		case strings.HasPrefix(line, "data:"):
			val := strings.TrimPrefix(line, "data:")
			val = strings.TrimPrefix(val, " ")
			if dataBuf.Len() > 0 {
				dataBuf.WriteByte('\n')
			}
			dataBuf.WriteString(val)
		}
		if result.Errored {
			break
		}
	}
	// Final flush — some servers don't trail with a blank line on close.
	flush()

	if err := scanner.Err(); err != nil {
		// Network drop mid-stream. Treat as turn drop per hermes.md §18 #5.
		slog.Warn("SSE read error mid-stream", "component", "hermes", "error", err)
		return result, fmt.Errorf("sse read: %w", err)
	}
	return result, nil
}
