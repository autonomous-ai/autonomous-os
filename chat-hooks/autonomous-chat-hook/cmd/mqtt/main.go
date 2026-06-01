// MQTT subscriber that reads chat messages published by the Autonomous BE
// (sent by users via the autonomous web UI) and forwards them into Lamp's
// sensing pipeline.
//
// Stand-alone process, mirroring twitch-chat-hook/cmd/irc/main.go. Output
// shape (HTTP POST to /api/sensing/event) is identical so downstream code
// sees the same sensing event format.
//
// Usage:
//
//	go run ./cmd/mqtt
//
// All configuration is via environment variables (see .env.example). The
// payload published by BE must be JSON of the shape:
//
//	{"user": "<display name>", "text": "<message>"}
//
// Unknown fields are ignored. If `user` is empty, "anonymous" is used.

package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/eclipse/paho.golang/autopaho"
	"github.com/eclipse/paho.golang/autopaho/queue/memory"
	"github.com/eclipse/paho.golang/paho"

	"autonomous-chat-hook/autonomous"
)

// Version is injected at build time via -ldflags "-X main.Version=...".
var Version = "dev"

type chatPayload struct {
	User string `json:"user"`
	Text string `json:"text"`
}

func main() {
	log.Printf("[autonomous-chat] version=%s", Version)

	brokerURL := os.Getenv("AUTONOMOUS_MQTT_URL")
	topic := os.Getenv("AUTONOMOUS_MQTT_TOPIC")
	if brokerURL == "" || topic == "" {
		log.Fatal("[autonomous-chat] AUTONOMOUS_MQTT_URL and AUTONOMOUS_MQTT_TOPIC are required")
	}

	u, err := url.Parse(brokerURL)
	if err != nil {
		log.Fatalf("[autonomous-chat] bad AUTONOMOUS_MQTT_URL: %v", err)
	}

	clientID := os.Getenv("AUTONOMOUS_MQTT_CLIENT_ID")
	if clientID == "" {
		clientID = randomClientID()
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	cfg := autopaho.ClientConfig{
		ServerUrls:                    []*url.URL{u},
		KeepAlive:                     30,
		CleanStartOnInitialConnection: true,
		SessionExpiryInterval:         60,
		ConnectTimeout:                10 * time.Second,
		ReconnectBackoff:              autopaho.DefaultExponentialBackoff(),
		Queue:                         memory.New(),
		OnConnectionUp: func(cm *autopaho.ConnectionManager, _ *paho.Connack) {
			log.Printf("[autonomous-chat] connected, subscribing topic=%s", topic)
			if _, err := cm.Subscribe(context.Background(), &paho.Subscribe{
				Subscriptions: []paho.SubscribeOptions{{Topic: topic, QoS: 1}},
			}); err != nil {
				log.Printf("[autonomous-chat] subscribe failed: %v", err)
			}
		},
		OnConnectionDown: func() bool { return true }, // keep reconnecting
		OnConnectError:   func(err error) { log.Printf("[autonomous-chat] connect error: %v", err) },
		ClientConfig: paho.ClientConfig{
			ClientID: clientID,
			OnPublishReceived: []func(paho.PublishReceived) (bool, error){
				func(pr paho.PublishReceived) (bool, error) {
					handleMessage(ctx, pr.Packet.Topic, pr.Packet.Payload)
					return true, nil
				},
			},
		},
	}
	if user := os.Getenv("AUTONOMOUS_MQTT_USERNAME"); user != "" {
		cfg.ConnectUsername = user
		cfg.ConnectPassword = []byte(os.Getenv("AUTONOMOUS_MQTT_PASSWORD"))
	}

	log.Printf("[autonomous-chat] connecting broker=%s client_id=%s", brokerURL, clientID)
	conn, err := autopaho.NewConnection(ctx, cfg)
	if err != nil {
		log.Fatalf("[autonomous-chat] new connection: %v", err)
	}

	if err := conn.AwaitConnection(ctx); err != nil && !errors.Is(err, context.Canceled) {
		log.Fatalf("[autonomous-chat] await connection: %v", err)
	}

	<-ctx.Done()
	log.Printf("[autonomous-chat] shutting down")

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()
	_ = conn.Disconnect(shutdownCtx)
}

func handleMessage(ctx context.Context, topic string, payload []byte) {
	var p chatPayload
	if err := json.Unmarshal(payload, &p); err != nil {
		log.Printf("[autonomous-chat] bad json on %s: %v", topic, err)
		return
	}
	if p.Text == "" {
		log.Printf("[autonomous-chat] empty text on %s, dropped", topic)
		return
	}
	log.Printf("[autonomous-chat] %s <%s> %s", topic, p.User, p.Text)
	autonomous.ForwardChatMessage(ctx, p.User, p.Text)
}

func randomClientID() string {
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		return "autonomous-chat-0"
	}
	return "autonomous-chat-" + hex.EncodeToString(b)
}
