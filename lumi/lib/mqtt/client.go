package mqtt

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log/slog"
	"net/url"
	"sync"
	"time"

	"github.com/eclipse/paho.golang/autopaho"
	"github.com/eclipse/paho.golang/autopaho/queue/memory"
	"github.com/eclipse/paho.golang/paho"
)

// ErrNotConnected is returned by Publish when the client is not connected.
var ErrNotConnected = errors.New("mqtt: not connected")

// MessageHandler is called when a message is received on a subscribed topic.
type MessageHandler func(topic string, payload []byte)

// MQTT is an MQTT client with auto-connect and reconnect.
type MQTT struct {
	opts       Options
	conn       *autopaho.ConnectionManager
	connCtx    context.Context
	connCancel context.CancelFunc
	mu         sync.Mutex
	// subscriptions are re-applied on each connection up
	subscriptions map[string]subEntry
	handlers      map[string]MessageHandler
}

type subEntry struct {
	topic string
	qos   byte
}

// ProvideClient creates an MQTT client from config. Endpoint (domain or IP) is required.
// Used by Wire (lib/mqtt.ProviderSet).
func ProvideClient(opts Options) *MQTT {
	if opts.KeepAlive == 0 {
		opts.KeepAlive = DefaultKeepAlive
	}
	if opts.ConnectTimeout == 0 {
		opts.ConnectTimeout = DefaultConnectTimeout
	}
	if opts.ClientID == "" {
		opts.ClientID = defaultClientID()
	}
	return &MQTT{
		opts:          opts,
		subscriptions: make(map[string]subEntry),
		handlers:      make(map[string]MessageHandler),
	}
}

func defaultClientID() string {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return "lumi-mqtt-0"
	}
	return "lumi-mqtt-" + hex.EncodeToString(b)
}

// Connect establishes the connection and starts auto-reconnect. It returns when the context
// is cancelled or after an initial connection failure. Run in a goroutine for long-lived use.
func (c *MQTT) Connect(ctx context.Context) error {
	c.mu.Lock()
	if c.conn != nil {
		c.mu.Unlock()
		return nil // already connected
	}
	c.connCtx, c.connCancel = context.WithCancel(ctx)
	connCtx := c.connCtx
	c.mu.Unlock()

	serverURL, err := c.opts.ServerURL()
	if err != nil {
		return err
	}

	cfg := autopaho.ClientConfig{
		ServerUrls:                    []*url.URL{serverURL},
		KeepAlive:                     c.opts.KeepAlive,
		CleanStartOnInitialConnection: true,
		SessionExpiryInterval:         60,
		ConnectTimeout:                c.opts.ConnectTimeout,
		ReconnectBackoff:              autopaho.DefaultExponentialBackoff(),
		Queue:                         memory.New(),
		OnConnectionUp: func(cm *autopaho.ConnectionManager, _ *paho.Connack) {
			c.mu.Lock()
			subs := c.copySubscriptions()
			c.mu.Unlock()
			slog.Info("mqtt connected", "component", "mqtt", "broker", c.opts.Endpoint, "subs", len(subs))
			for _, s := range subs {
				if _, err := cm.Subscribe(context.Background(), &paho.Subscribe{
					Subscriptions: []paho.SubscribeOptions{{Topic: s.topic, QoS: s.qos}},
				}); err != nil {
					slog.Error("subscribe failed", "component", "mqtt", "topic", s.topic, "error", err)
				} else {
					slog.Info("subscribed ok", "component", "mqtt", "topic", s.topic)
				}
			}
		},
		OnConnectionDown: func() bool { return true }, // keep reconnecting
		OnConnectError:   func(err error) { slog.Error("connect error", "component", "mqtt", "error", err) },
		ClientConfig: paho.ClientConfig{
			ClientID: c.opts.ClientID,
			OnPublishReceived: []func(paho.PublishReceived) (bool, error){
				func(pr paho.PublishReceived) (bool, error) {
					c.mu.Lock()
					fn := c.handlers[pr.Packet.Topic]
					c.mu.Unlock()
					if fn != nil {
						fn(pr.Packet.Topic, pr.Packet.Payload)
					}
					return true, nil
				},
			},
		},
	}
	if c.opts.Username != "" {
		cfg.ConnectUsername = c.opts.Username
		cfg.ConnectPassword = []byte(c.opts.Password)
	}

	conn, err := autopaho.NewConnection(connCtx, cfg)
	if err != nil {
		return err
	}

	c.mu.Lock()
	c.conn = conn
	c.mu.Unlock()

	return conn.AwaitConnection(connCtx)
}

func (c *MQTT) copySubscriptions() []subEntry {
	var out []subEntry
	for _, s := range c.subscriptions {
		out = append(out, s)
	}
	return out
}

// Close disconnects and stops the client.
func (c *MQTT) Close() error {
	c.mu.Lock()
	conn := c.conn
	cancel := c.connCancel
	c.conn = nil
	c.connCancel = nil
	c.mu.Unlock()

	if cancel != nil {
		cancel()
	}
	if conn != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = conn.Disconnect(ctx)
	}
	return nil
}

// Subscribe registers a topic subscription and handler. Subscriptions are re-applied on reconnect.
// Call before Connect, or after Connect (handler will be used for new messages; re-subscribe requires re-connect or internal re-sub logic).
func (c *MQTT) Subscribe(topic string, qos byte, handler MessageHandler) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.subscriptions[topic] = subEntry{topic: topic, qos: qos}
	if handler != nil {
		c.handlers[topic] = handler
	}
}

// Publish sends a message. Returns when the message is sent or the context is cancelled.
func (c *MQTT) Publish(ctx context.Context, topic string, qos byte, payload []byte) error {
	c.mu.Lock()
	conn := c.conn
	c.mu.Unlock()
	if conn == nil {
		return ErrNotConnected
	}
	_, err := conn.Publish(ctx, &paho.Publish{
		Topic:   topic,
		QoS:     qos,
		Payload: payload,
	})
	return err
}

// Done returns a channel that is closed when the connection manager has shut down.
func (c *MQTT) Done() <-chan struct{} {
	c.mu.Lock()
	conn := c.conn
	c.mu.Unlock()
	if conn == nil {
		ch := make(chan struct{})
		close(ch)
		return ch
	}
	return conn.Done()
}
