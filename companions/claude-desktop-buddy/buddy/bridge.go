package buddy

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"
)

// claudeBrand is the Claude app icon color (#C15F3C) used for all
// buddy state LED cues so the device visibly speaks "Claude".
var claudeBrand = [3]int{193, 95, 60}

// Bridge maps buddy state changes to device and OS server HTTP calls.
type Bridge struct {
	halURL    string
	deviceURL string
	client    *http.Client
}

func NewBridge(halURL, deviceURL string) *Bridge {
	return &Bridge{
		halURL:    halURL,
		deviceURL: deviceURL,
		client:    &http.Client{Timeout: 5 * time.Second},
	}
}

// OnStateChange is called by StateMachine when state transitions.
func (b *Bridge) OnStateChange(old, next BuddyState, hb *Heartbeat) {
	log.Printf("[bridge] %s → %s", old, next)

	switch next {
	case StateSleep:
		// CHANGED 2026-05-26: was b.ledOff() — that killed the user's ambient LED
		// when Claude disconnected (user's blue → off forever until Buddy reconnects).
		// Sleep = Buddy stops being the indicator → hand strip back to user, matching
		// StateIdle behavior below.
		b.ledRestore()
		b.displayEyes("sleepy")

	case StateIdle:
		// Released the strip — hand it back to the user's saved LED state.
		// Skip when coming from Busy: main.go fires an emotion right
		// after, and that emotion has its own restore. Avoids a
		// user-color flash between Busy's effect and the emotion.
		if old != StateBusy {
			b.ledRestore()
		}
		b.displayEyesMode()

	case StateBusy:
		b.ledEffect("pulse", claudeBrand, 0.8, 0)
		if hb != nil {
			b.displayInfo(
				fmt.Sprintf("%s tokens", formatTokens(hb.TokensToday)),
				fmt.Sprintf("%d sessions running", hb.Running),
			)
		}

	case StateAttention:
		b.ledEffect("blink", claudeBrand, 1.5, 0)
		if hb != nil && hb.Prompt != nil {
			b.displayInfo(
				fmt.Sprintf("Approve %s?", hb.Prompt.Tool),
				truncate(hb.Prompt.Hint, 40),
			)
			b.postSensingEvent(hb.Prompt)
		}

	case StateHeart:
		b.ledSolid(claudeBrand)
		b.displayEyes("happy")

	case StateCelebrate:
		b.ledEffect("rainbow", claudeBrand, 2.0, 3000)
		b.displayEyes("excited")
	}

	b.postBuddyState(next, hb)
}

// --- Device calls (port 5001) ---
//
// All LED writes from Buddy are marked transient: they paint the strip
// but don't overwrite the user's saved LED state (e.g. "đèn xanh lá").
// When Buddy returns to Idle, ledRestore() asks the device to repaint
// whatever the user had set before Buddy took the strip.

func (b *Bridge) ledOff() {
	b.post(b.halURL+"/led/off", map[string]interface{}{
		"transient": true,
	})
}

func (b *Bridge) ledSolid(color [3]int) {
	b.post(b.halURL+"/led/solid", map[string]interface{}{
		"color":     color,
		"transient": true,
	})
}

func (b *Bridge) ledEffect(effect string, color [3]int, speed float64, durationMs int) {
	payload := map[string]interface{}{
		"effect":    effect,
		"color":     color,
		"speed":     speed,
		"transient": true,
	}
	if durationMs > 0 {
		payload["duration_ms"] = durationMs
	}
	b.post(b.halURL+"/led/effect", payload)
}

func (b *Bridge) ledRestore() {
	b.post(b.halURL+"/led/restore", nil)
}

func (b *Bridge) displayInfo(text, subtitle string) {
	b.post(b.halURL+"/display/info", map[string]interface{}{
		"text":     text,
		"subtitle": subtitle,
	})
}

func (b *Bridge) displayEyes(expression string) {
	b.post(b.halURL+"/display/eyes", map[string]interface{}{
		"expression": expression,
	})
}

func (b *Bridge) displayEyesMode() {
	b.post(b.halURL+"/display/eyes-mode", nil)
}

// --- OS server calls (port 5000) ---

// postBuddyState sends buddy state to the OS server monitor bus.
func (b *Bridge) postBuddyState(state BuddyState, hb *Heartbeat) {
	detail := map[string]interface{}{
		"state": string(state),
	}
	if hb != nil && hb.Prompt != nil {
		detail["tool"] = hb.Prompt.Tool
		detail["hint"] = hb.Prompt.Hint
	}

	b.post(b.deviceURL+"/api/monitor/event", map[string]interface{}{
		"type":    "buddy_state",
		"summary": fmt.Sprintf("buddy: %s", state),
		"detail":  detail,
	})
}

// postSensingEvent sends approval event to the OS server sensing pipeline.
func (b *Bridge) postSensingEvent(prompt *Prompt) {
	b.post(b.deviceURL+"/api/sensing/event", map[string]interface{}{
		"type":    "buddy_approval",
		"message": fmt.Sprintf("Claude Desktop needs approval: %s on %s [prompt_id:%s]", prompt.Tool, prompt.Hint, prompt.ID),
	})
}

// expressEmotion triggers a coordinated LED + servo animation on
// the device. Used by the buddy state listener to celebrate the end of a
// Claude turn ("Claude is done" → happy emotion). The device owns the
// LED/servo timeline from there so we don't fight its ambient logic.
func (b *Bridge) expressEmotion(name string, intensity float64) {
	if name == "" {
		return
	}
	b.post(b.halURL+"/emotion", map[string]interface{}{
		"emotion":   name,
		"intensity": intensity,
	})
}

// prerenderTTS asks the device to synthesize a phrase and store it in the
// on-disk TTS cache without playing it. Used at startup to warm the
// cache for every narration phrase the device will need, so the very
// first announcement of each one plays instantly instead of waiting
// on a provider round-trip.
func (b *Bridge) prerenderTTS(text string) {
	if text == "" {
		return
	}
	b.post(b.halURL+"/voice/speak", map[string]interface{}{
		"text":      text,
		"prerender": true,
	})
}

// speakTTS posts a short narration string to the device's TTS endpoint
// (POST /voice/speak). Fire-and-forget: the device rejects with 409 when
// music is playing or 503 when TTS isn't initialized; both responses
// are ignored at the bridge layer so callers (mostly the Narrator)
// don't have to coordinate with the voice pipeline.
//
// `cached: true` tells the device to look the text up in its on-disk TTS
// cache before calling the provider. Narration phrases are a small,
// repetitive set ("Đang sửa file", "Xong", …) so after each phrase
// has been spoken once the rest of the day hits the cache — zero
// extra TTS API cost and near-instant playback.
func (b *Bridge) speakTTS(text string) {
	if text == "" {
		return
	}
	b.post(b.halURL+"/voice/speak", map[string]interface{}{
		"text":   text,
		"cached": true,
	})
}

// OnEvent forwards a parsed Event (chat turn etc.) to the OS server so use cases
// like "speak Claude's reply" or "show recent message on display" can
// subscribe to /api/monitor/event with type=buddy_event. The bridge is
// purely fire-and-forget; downstream consumers decide whether to do
// anything with the payload.
func (b *Bridge) OnEvent(evt *Event) {
	if evt == nil {
		return
	}
	b.post(b.deviceURL+"/api/monitor/event", map[string]interface{}{
		"type":    "buddy_event",
		"summary": fmt.Sprintf("buddy %s %s", evt.Evt, evt.Role),
		"detail": map[string]interface{}{
			"evt":     evt.Evt,
			"role":    evt.Role,
			"content": evt.TurnText(),
		},
	})
}

// --- Helpers ---

func (b *Bridge) post(url string, payload interface{}) {
	var body []byte
	if payload != nil {
		var err error
		body, err = json.Marshal(payload)
		if err != nil {
			log.Printf("[bridge] marshal error for %s: %v", url, err)
			return
		}
	}

	var resp *http.Response
	var err error
	if body != nil {
		resp, err = b.client.Post(url, "application/json", bytes.NewReader(body))
	} else {
		resp, err = b.client.Post(url, "application/json", nil)
	}
	if err != nil {
		log.Printf("[bridge] %s error: %v", url, err)
		return
	}
	resp.Body.Close()
}

func formatTokens(n int) string {
	if n >= 1000 {
		return fmt.Sprintf("%.1fK", float64(n)/1000)
	}
	return fmt.Sprintf("%d", n)
}

func truncate(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max-3] + "..."
}
