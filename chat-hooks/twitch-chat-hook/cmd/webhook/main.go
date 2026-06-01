// Webhook receiver for Twitch EventSub channel.chat.message.
//
// Env:
//   TWITCH_WEBHOOK_SECRET  - same secret used when creating the subscription
//   PORT                   - listen port (default 8080)
//
// In production, terminate TLS in front (nginx/ALB/Caddy). Twitch requires
// HTTPS for the callback URL but the Go server itself can speak plain HTTP
// behind a reverse proxy.

package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"twitch-chat-hook/twitch"
)

func main() {
	secret := os.Getenv("TWITCH_WEBHOOK_SECRET")
	if secret == "" {
		log.Fatal("TWITCH_WEBHOOK_SECRET is required")
	}
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	h := &handler{
		secret: []byte(secret),
		seen:   newDedupe(10 * time.Minute),
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/twitch/webhook", h.serveEventSub)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
	}

	go func() {
		log.Printf("[twitch-webhook] listening on :%s", port)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("[twitch-webhook] server: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
}

type handler struct {
	secret []byte
	seen   *dedupe
}

func (h *handler) serveEventSub(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20)) // 1 MiB cap
	if err != nil {
		http.Error(w, "read body", http.StatusBadRequest)
		return
	}

	if err := twitch.VerifySignature(r.Header, body, h.secret); err != nil {
		log.Printf("[twitch-webhook] reject: %v", err)
		http.Error(w, "invalid signature", http.StatusForbidden)
		return
	}

	// Idempotency — Twitch retries on non-2xx and may also redeliver.
	id := r.Header.Get(twitch.HeaderMessageID)
	if h.seen.checkAndAdd(id) {
		w.WriteHeader(http.StatusNoContent)
		return
	}

	var env twitch.Envelope
	if err := json.Unmarshal(body, &env); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}

	switch r.Header.Get(twitch.HeaderMessageType) {
	case twitch.MsgTypeVerification:
		// One-shot handshake — echo the challenge as plain text.
		w.Header().Set("Content-Type", "text/plain")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(env.Challenge))
		log.Printf("[twitch-webhook] verified subscription %s (%s)", env.Subscription.ID, env.Subscription.Type)

	case twitch.MsgTypeRevocation:
		log.Printf("[twitch-webhook] subscription revoked: id=%s status=%s type=%s",
			env.Subscription.ID, env.Subscription.Status, env.Subscription.Type)
		w.WriteHeader(http.StatusNoContent)

	case twitch.MsgTypeNotification:
		if err := h.dispatch(r.Context(), env); err != nil {
			// 2xx anyway — Twitch will retry on non-2xx and that usually
			// is not what you want when your downstream is the problem.
			// Log it and ack. Adjust if you'd rather take retries.
			log.Printf("[twitch-webhook] dispatch error: %v", err)
		}
		w.WriteHeader(http.StatusNoContent)

	default:
		http.Error(w, "unknown message type", http.StatusBadRequest)
	}
}

func (h *handler) dispatch(ctx context.Context, env twitch.Envelope) error {
	switch env.Subscription.Type {
	case "channel.chat.message":
		var ev twitch.ChatMessageEvent
		if err := json.Unmarshal(env.Event, &ev); err != nil {
			return err
		}
		return handleChatMessage(ctx, ev)
	default:
		log.Printf("[twitch-webhook] ignoring unhandled type: %s", env.Subscription.Type)
		return nil
	}
}

// handleChatMessage logs the chat line and forwards it to Lamp's sensing
// endpoint, mirroring LeLamp's voice_service.py — same URL, same body
// shape, with a "[source: twitch]" prefix so SOUL.md can route it.
func handleChatMessage(ctx context.Context, ev twitch.ChatMessageEvent) error {
	log.Printf("[twitch-chat] #%s <%s> %s",
		ev.BroadcasterUserLogin, ev.ChatterUserLogin, ev.Message.Text)
	twitch.ForwardChatMessage(ctx, ev.ChatterUserLogin, ev.Message.Text)
	return nil
}

// dedupe keeps recent message IDs in memory to drop duplicate deliveries.
// For multi-instance deployments swap this for Redis SETNX with TTL.
type dedupe struct {
	mu  sync.Mutex
	ttl time.Duration
	ids map[string]time.Time
}

func newDedupe(ttl time.Duration) *dedupe {
	return &dedupe{ttl: ttl, ids: make(map[string]time.Time)}
}

func (d *dedupe) checkAndAdd(id string) bool {
	if id == "" {
		return false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	now := time.Now()
	// Lazy GC.
	for k, t := range d.ids {
		if now.Sub(t) > d.ttl {
			delete(d.ids, k)
		}
	}
	if _, ok := d.ids[id]; ok {
		return true
	}
	d.ids[id] = now
	return false
}
