// Forwards autonomous-web chat messages into Lamp's sensing pipeline, the
// same way twitch-chat-hook/twitch/forward.go forwards Twitch chat. The
// "[source: autonomous_web]" prefix lets SOUL.md tell BE-driven messages
// apart from real microphone input.

package autonomous

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"
)

const (
	defaultLampSensingURL = "http://127.0.0.1:5000/api/sensing/event"
	defaultEventType      = "voice"
)

var forwardClient = &http.Client{Timeout: 2 * time.Second}

type sensingEvent struct {
	Type    string `json:"type"`
	Message string `json:"message"`
}

// ForwardChatMessage POSTs a chat line to the Lamp sensing endpoint.
//
// Env overrides:
//
//	LAMP_SENSING_URL         default http://127.0.0.1:5000/api/sensing/event
//	AUTONOMOUS_SENSING_TYPE  default voice
//
// Fire-and-forget. The send runs in a background goroutine so the MQTT
// receive loop is never blocked by a slow Lamp; errors are logged.
func ForwardChatMessage(ctx context.Context, user, text string) {
	go forward(ctx, user, text)
}

func forward(ctx context.Context, user, text string) {
	url := os.Getenv("LAMP_SENSING_URL")
	if url == "" {
		url = defaultLampSensingURL
	}
	evtType := os.Getenv("AUTONOMOUS_SENSING_TYPE")
	if evtType == "" {
		evtType = defaultEventType
	}

	if user == "" {
		user = "anonymous"
	}

	body, err := json.Marshal(sensingEvent{
		Type:    evtType,
		Message: fmt.Sprintf("[source: autonomous_web, user: %s] %s", user, text),
	})
	if err != nil {
		log.Printf("[autonomous-forward] marshal: %v", err)
		return
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		log.Printf("[autonomous-forward] build request: %v", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := forwardClient.Do(req)
	if err != nil {
		if ctx.Err() == nil {
			log.Printf("[autonomous-forward] post: %v", err)
		}
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		log.Printf("[autonomous-forward] non-2xx %s for <%s>", resp.Status, user)
	}
}
