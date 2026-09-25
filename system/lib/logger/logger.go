package logger

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"gopkg.in/natefinch/lumberjack.v2"
)

// GELF centralized logging. Read inside Init() so callers can load .env
// (godotenv) before logging is initialized. If GELF_URL is empty, the GELF
// handler is attached dormant and ships nothing until EnableGELFRelay arms it.
var (
	gelfURL      string
	gelfUsername string
	gelfPassword string
)

// ANSI color codes
const (
	colorReset  = "\033[0m"
	colorRed    = "\033[31m"
	colorGreen  = "\033[32m"
	colorYellow = "\033[33m"
	colorCyan   = "\033[36m"
	colorGray   = "\033[90m"
)

// colorHandler is a slog.Handler that writes colored, human-readable log lines to the console.
type colorHandler struct {
	w     io.Writer
	mu    sync.Mutex
	level slog.Level
	attrs []slog.Attr
	group string
}

func (h *colorHandler) Enabled(_ context.Context, level slog.Level) bool {
	return level >= h.level
}

func (h *colorHandler) Handle(_ context.Context, r slog.Record) error {
	var levelColor, levelTag string
	switch {
	case r.Level >= slog.LevelError:
		levelColor = colorRed
		levelTag = "ERROR"
	case r.Level >= slog.LevelWarn:
		levelColor = colorYellow
		levelTag = "WARN"
	case r.Level >= slog.LevelInfo:
		levelColor = colorGreen
		levelTag = "INFO"
	default:
		levelColor = colorGray
		levelTag = "DEBUG"
	}

	ts := r.Time.Format(time.DateTime)
	line := fmt.Sprintf("%s%s%s %s%-5s%s %s",
		colorGray, ts, colorReset,
		levelColor, levelTag, colorReset,
		r.Message,
	)

	// Append attributes
	r.Attrs(func(a slog.Attr) bool {
		key := a.Key
		if h.group != "" {
			key = h.group + "." + key
		}
		line += fmt.Sprintf(" %s%s=%s%v", colorCyan, key, colorReset, a.Value)
		return true
	})
	for _, a := range h.attrs {
		key := a.Key
		if h.group != "" {
			key = h.group + "." + key
		}
		line += fmt.Sprintf(" %s%s=%s%v", colorCyan, key, colorReset, a.Value)
	}
	line += "\n"

	h.mu.Lock()
	defer h.mu.Unlock()
	_, err := h.w.Write([]byte(line))
	return err
}

func (h *colorHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	newAttrs := make([]slog.Attr, len(h.attrs), len(h.attrs)+len(attrs))
	copy(newAttrs, h.attrs)
	newAttrs = append(newAttrs, attrs...)
	return &colorHandler{w: h.w, level: h.level, attrs: newAttrs, group: h.group}
}

func (h *colorHandler) WithGroup(name string) slog.Handler {
	g := name
	if h.group != "" {
		g = h.group + "." + name
	}
	newAttrs := make([]slog.Attr, len(h.attrs))
	copy(newAttrs, h.attrs)
	return &colorHandler{w: h.w, level: h.level, attrs: newAttrs, group: g}
}

// multiHandler fans out each log record to multiple handlers.
type multiHandler struct {
	handlers []slog.Handler
}

func (m *multiHandler) Enabled(ctx context.Context, level slog.Level) bool {
	for _, h := range m.handlers {
		if h.Enabled(ctx, level) {
			return true
		}
	}
	return false
}

func (m *multiHandler) Handle(ctx context.Context, r slog.Record) error {
	for _, h := range m.handlers {
		if h.Enabled(ctx, r.Level) {
			if err := h.Handle(ctx, r); err != nil {
				return err
			}
		}
	}
	return nil
}

func (m *multiHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	handlers := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		handlers[i] = h.WithAttrs(attrs)
	}
	return &multiHandler{handlers: handlers}
}

func (m *multiHandler) WithGroup(name string) slog.Handler {
	handlers := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		handlers[i] = h.WithGroup(name)
	}
	return &multiHandler{handlers: handlers}
}

const (
	gelfQueueSize            = 256
	gelfShutdownFlushTimeout = 5 * time.Second
	// gelfRelayPath is appended to the cloud API base URL (which already ends in
	// /v1) to reach its GELF relay.
	gelfRelayPath = "/logs/gelf"
)

// gelfSender owns the bounded GELF delivery queue. Logging must never create an
// unbounded number of network goroutines when the remote collector is slow or
// unavailable, so overload drops the newest GELF record instead. Drops are
// reported to stderr with exponentially spaced notices to preserve observability
// without turning a collector outage into a second log storm.
type gelfSender struct {
	client *http.Client
	url    string
	// auth sets the request credential: basic auth for a direct collector, the
	// device's Bearer key for the cloud API relay. Nil sends none.
	auth func(*http.Request)

	queue   chan []byte
	ctx     context.Context
	cancel  context.CancelFunc
	done    chan struct{}
	mu      sync.RWMutex
	closed  bool
	dropped atomic.Uint64
}

func newGELFSender(client *http.Client, url string, auth func(*http.Request)) *gelfSender {
	ctx, cancel := context.WithCancel(context.Background())
	s := &gelfSender{
		client: client,
		url:    url,
		auth:   auth,
		queue:  make(chan []byte, gelfQueueSize),
		ctx:    ctx,
		cancel: cancel,
		done:   make(chan struct{}),
	}
	go s.run()
	return s
}

// basicAuth is the direct-collector credential (GELF_USERNAME/GELF_PASSWORD).
// Nil without a username, which sends no credential at all.
func basicAuth(username, password string) func(*http.Request) {
	if username == "" {
		return nil
	}
	return func(r *http.Request) { r.SetBasicAuth(username, password) }
}

// bearerAuth is the relay credential: the device's lobster API key.
func bearerAuth(key string) func(*http.Request) {
	return func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+key) }
}

func (s *gelfSender) enqueue(body []byte) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.closed {
		return
	}
	select {
	case s.queue <- body:
	default:
		dropped := s.dropped.Add(1)
		if dropped == 1 || dropped&(dropped-1) == 0 {
			fmt.Fprintf(os.Stderr, "[gelf] queue full; dropped %d record(s)\n", dropped)
		}
	}
}

func (s *gelfSender) run() {
	defer close(s.done)
	for {
		select {
		case <-s.ctx.Done():
			return
		case body, ok := <-s.queue:
			if !ok {
				return
			}
			s.send(body)
		}
	}
}

func (s *gelfSender) send(body []byte) {
	req, err := http.NewRequestWithContext(s.ctx, http.MethodPost, s.url, bytes.NewReader(body))
	if err != nil {
		fmt.Fprintf(os.Stderr, "[gelf] request error: %v\n", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	if s.auth != nil {
		s.auth(req)
	}
	resp, err := s.client.Do(req)
	if err != nil {
		if s.ctx.Err() == nil {
			fmt.Fprintf(os.Stderr, "[gelf] send error: %v\n", err)
		}
		return
	}
	resp.Body.Close()
}

func (s *gelfSender) close() {
	defer s.cancel()
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	s.closed = true
	close(s.queue)
	s.mu.Unlock()

	timer := time.NewTimer(gelfShutdownFlushTimeout)
	defer timer.Stop()
	select {
	case <-s.done:
		return
	case <-timer.C:
		remaining := len(s.queue)
		s.cancel()
		<-s.done
		fmt.Fprintf(os.Stderr, "[gelf] shutdown flush timed out; dropped %d queued record(s)\n", remaining)
	}
}

// gelfSink is the delivery slot shared by a GELF handler and every handler
// derived from it. slog's With/WithGroup copy the handler, so a sender held by
// value would leave loggers built before the relay was armed — package-level
// ones are built long before config.json loads — pointing at nil forever.
// Holding it behind one shared pointer arms all of them at once.
type gelfSink struct {
	sender atomic.Pointer[gelfSender]
}

func (s *gelfSink) load() *gelfSender { return s.sender.Load() }

// gelfHandler serializes records and enqueues them for the shared GELF sender.
// It is dormant (Enabled reports false, nothing is serialized) until its sink
// holds a sender.
type gelfHandler struct {
	level      slog.Level
	host       string
	deviceType string
	client     *http.Client
	sink       *gelfSink
	attrs      []slog.Attr
	group      string
}

// newGELFHandler returns a handler delivering straight to GELF_URL.
func newGELFHandler(level slog.Level, host string) *gelfHandler {
	h := newDormantGELFHandler(level, host)
	h.sink.sender.Store(newGELFSender(h.client, gelfURL, basicAuth(gelfUsername, gelfPassword)))
	return h
}

// newDormantGELFHandler returns a handler with no sender, and so no goroutine,
// for EnableGELFRelay to arm once the device key is known. A process that never
// arms it (bootstrap) ships nothing and pays nothing.
func newDormantGELFHandler(level slog.Level, host string) *gelfHandler {
	return &gelfHandler{
		level:  level,
		host:   host,
		client: &http.Client{Timeout: 3 * time.Second},
		sink:   &gelfSink{},
	}
}

func (h *gelfHandler) Enabled(_ context.Context, level slog.Level) bool {
	return level >= h.level && h.sink.load() != nil
}

func slogLevelToGELF(level slog.Level) int {
	switch {
	case level >= slog.LevelError:
		return 3 // error
	case level >= slog.LevelWarn:
		return 4 // warning
	case level >= slog.LevelInfo:
		return 6 // info
	default:
		return 7 // debug
	}
}

func (h *gelfHandler) Handle(_ context.Context, r slog.Record) error {
	sender := h.sink.load()
	if sender == nil {
		return nil // dormant: nowhere to ship yet
	}

	msg := map[string]any{
		"version":       "1.1",
		"host":          h.host,
		"short_message": r.Message,
		"timestamp":     float64(r.Time.UnixNano()) / 1e9,
		"level":         slogLevelToGELF(r.Level),
		"_service_name": "os-server",
		"_level_name":   r.Level.String(),
		"_pid":          os.Getpid(),
	}
	if h.deviceType != "" {
		msg["_device_type"] = h.deviceType // device class, for centralized filtering
	}

	// Add attributes as GELF extra fields (prefixed with _)
	for _, a := range h.attrs {
		key := a.Key
		if h.group != "" {
			key = h.group + "." + key
		}
		msg["_"+key] = a.Value.String()
	}
	r.Attrs(func(a slog.Attr) bool {
		key := a.Key
		if h.group != "" {
			key = h.group + "." + key
		}
		msg["_"+key] = a.Value.String()
		return true
	})

	body, err := json.Marshal(msg)
	if err != nil {
		return nil // don't block on marshal errors
	}

	sender.enqueue(body)

	return nil
}

func (h *gelfHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	newAttrs := make([]slog.Attr, len(h.attrs), len(h.attrs)+len(attrs))
	copy(newAttrs, h.attrs)
	newAttrs = append(newAttrs, attrs...)
	return &gelfHandler{level: h.level, host: h.host, client: h.client, sink: h.sink, attrs: newAttrs, group: h.group}
}

func (h *gelfHandler) WithGroup(name string) slog.Handler {
	g := name
	if h.group != "" {
		g = h.group + "." + name
	}
	newAttrs := make([]slog.Attr, len(h.attrs))
	copy(newAttrs, h.attrs)
	return &gelfHandler{level: h.level, host: h.host, client: h.client, sink: h.sink, attrs: newAttrs, group: g}
}

// activeGELF holds the GELF handler so SetGELFHost can update host after config loads.
var activeGELF *gelfHandler

// SetGELFHost updates the GELF host field (call after config is loaded with device_id).
func SetGELFHost(host string) {
	if activeGELF != nil && host != "" {
		activeGELF.host = host
	}
}

// SetGELFDeviceType stamps the device class on every shipped log as `_device_type`
// (call after config loads) so centralized logs are filterable by device, not the
// per-unit host. Device-agnostic: each device reports its own class, not "lamp".
func SetGELFDeviceType(deviceType string) {
	if activeGELF != nil && deviceType != "" {
		activeGELF.deviceType = deviceType
	}
}

// EnableGELFRelay arms the dormant GELF handler to ship through
// the cloud API instead of straight to the collector: POST {baseURL}/logs/gelf
// with the device's own key as a Bearer token. The collector credential stays
// server-side, so the device carries none — shipped devices are not provisioned
// with GELF_URL/GELF_USERNAME/GELF_PASSWORD.
//
// Call once config.json has loaded. No-op when GELF_URL is set (direct delivery
// wins), when baseURL or apiKey is blank, when Init attached no GELF handler, or
// when the relay is already armed, so a repeat call cannot start a second sender.
func EnableGELFRelay(baseURL, apiKey string) {
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	apiKey = strings.TrimSpace(apiKey)
	h := activeGELF
	if h == nil || baseURL == "" || apiKey == "" || h.sink.load() != nil {
		return
	}
	sender := newGELFSender(h.client, baseURL+gelfRelayPath, bearerAuth(apiKey))
	if !h.sink.sender.CompareAndSwap(nil, sender) {
		sender.close() // lost a race with a concurrent call; keep the winner
	}
}

// Init sets up the global slog default logger with colored console output.
// HAL_LOG_LEVEL controls the level for the Go services and HAL from the shared
// /opt/hal/.env. Missing or invalid values default to INFO.
// If logFilePath is non-empty, logs are also written to that file (plain text, no color).
// Returns a cleanup function to close the log file (call via defer).
func Init(logFilePath string) func() {
	level := levelFromEnv()

	consoleHandler := &colorHandler{
		w:     os.Stdout,
		level: level,
	}

	if logFilePath == "" {
		slog.SetDefault(slog.New(consoleHandler))
		return func() {}
	}

	// Rotating log file: 2 MB per file, keep 10 most recent backups
	rotatingWriter := &lumberjack.Logger{
		Filename:   logFilePath,
		MaxSize:    2, // MB
		MaxBackups: 10,
		MaxAge:     0, // no age-based removal
		Compress:   false,
	}

	fileHandler := &colorHandler{
		w:     rotatingWriter,
		level: level,
	}

	gelfURL = os.Getenv("GELF_URL")
	gelfUsername = os.Getenv("GELF_USERNAME")
	gelfPassword = os.Getenv("GELF_PASSWORD")

	// GELF_URL ships straight to that collector. Without it the handler is
	// attached dormant for EnableGELFRelay to arm once config.json supplies the
	// device key; until then (and forever, in bootstrap) it ships nothing.
	// "os-server" is the pre-config host; SetGELFHost(DeviceID) overrides it.
	var gelf *gelfHandler
	if gelfURL != "" {
		gelf = newGELFHandler(level, "os-server")
	} else {
		gelf = newDormantGELFHandler(level, "os-server")
	}
	activeGELF = gelf

	slog.SetDefault(slog.New(&multiHandler{handlers: []slog.Handler{consoleHandler, fileHandler, gelf}}))

	return func() {
		if activeGELF == gelf {
			activeGELF = nil
		}
		if sender := gelf.sink.load(); sender != nil {
			sender.close()
		}
		rotatingWriter.Close()
	}
}

func levelFromEnv() slog.Level {
	switch strings.ToUpper(strings.TrimSpace(os.Getenv("HAL_LOG_LEVEL"))) {
	case "DEBUG":
		return slog.LevelDebug
	case "INFO":
		return slog.LevelInfo
	case "WARN", "WARNING":
		return slog.LevelWarn
	case "ERROR":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
