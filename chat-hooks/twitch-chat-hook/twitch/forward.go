// Forwards Twitch chat lines into Lamp's sensing pipeline as a sensing
// event, mirroring how LeLamp's voice service posts voice_command
// transcripts (see lelamp/service/voice/voice_service.py: same URL, same
// body shape). The "[source: twitch]" prefix lets SOUL.md distinguish this
// from real microphone input.

package twitch

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
//	LAMP_SENSING_URL     default http://127.0.0.1:5000/api/sensing/event
//	TWITCH_SENSING_TYPE  default voice_command
//
// Fire-and-forget. The send runs in a background goroutine so the caller's
// read loop is never blocked by a slow Lamp; errors are logged.
func ForwardChatMessage(ctx context.Context, nick, text string) {
	go forward(ctx, nick, text)
}

func forward(ctx context.Context, nick, text string) {
	url := os.Getenv("LAMP_SENSING_URL")
	if url == "" {
		url = defaultLampSensingURL
	}
	evtType := os.Getenv("TWITCH_SENSING_TYPE")
	if evtType == "" {
		evtType = defaultEventType
	}

	body, err := json.Marshal(sensingEvent{
		Type:    evtType,
		Message: fmt.Sprintf("[source: twitch, twitch_user: %s] %s", nick, text),
	})
	if err != nil {
		log.Printf("[twitch-forward] marshal: %v", err)
		return
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		log.Printf("[twitch-forward] build request: %v", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := forwardClient.Do(req)
	if err != nil {
		if ctx.Err() == nil {
			log.Printf("[twitch-forward] post: %v", err)
		}
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		log.Printf("[twitch-forward] non-2xx %s for <%s>", resp.Status, nick)
	}
}
