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
	"sync"
	"time"

	"gopkg.in/natefinch/lumberjack.v2"
)

// GELF centralized logging. Read inside Init() so callers can load .env
// (godotenv) before logging is initialized. If GELF_URL is empty, the GELF
// handler is not attached.
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

// gelfHandler sends log records to a centralized GELF endpoint over HTTP.
type gelfHandler struct {
	level      slog.Level
	host       string
	deviceType string
	client     *http.Client
	attrs      []slog.Attr
	group      string
}

func newGELFHandler(level slog.Level, host string) *gelfHandler {
	return &gelfHandler{
		level:  level,
		host:   host,
		client: &http.Client{Timeout: 3 * time.Second},
	}
}

func (h *gelfHandler) Enabled(_ context.Context, level slog.Level) bool {
	return level >= h.level
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

	go func() {
		req, err := http.NewRequest("POST", gelfURL, bytes.NewReader(body))
		if err != nil {
			fmt.Fprintf(os.Stderr, "[gelf] request error: %v\n", err)
			return
		}
		req.Header.Set("Content-Type", "application/json")
		if gelfUsername != "" {
			req.SetBasicAuth(gelfUsername, gelfPassword)
		}
		resp, err := h.client.Do(req)
		if err != nil {
			fmt.Fprintf(os.Stderr, "[gelf] send error: %v\n", err)
			return
		}
		resp.Body.Close()
	}()

	return nil
}

func (h *gelfHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	newAttrs := make([]slog.Attr, len(h.attrs), len(h.attrs)+len(attrs))
	copy(newAttrs, h.attrs)
	newAttrs = append(newAttrs, attrs...)
	return &gelfHandler{level: h.level, host: h.host, client: h.client, attrs: newAttrs, group: h.group}
}

func (h *gelfHandler) WithGroup(name string) slog.Handler {
	g := name
	if h.group != "" {
		g = h.group + "." + name
	}
	newAttrs := make([]slog.Attr, len(h.attrs))
	copy(newAttrs, h.attrs)
	return &gelfHandler{level: h.level, host: h.host, client: h.client, attrs: newAttrs, group: g}
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

// Init sets up the global slog default logger with colored console output.
// If logFilePath is non-empty, logs are also written to that file (plain text, no color).
// Returns a cleanup function to close the log file (call via defer).
func Init(level slog.Level, logFilePath string) func() {
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

	handlers := []slog.Handler{consoleHandler, fileHandler}
	if gelfURL != "" {
		gelf := newGELFHandler(slog.LevelInfo, "os-server") // pre-config host; SetGELFHost(DeviceID) overrides once config loads
		activeGELF = gelf
		handlers = append(handlers, gelf)
	}

	slog.SetDefault(slog.New(&multiHandler{handlers: handlers}))

	return func() { rotatingWriter.Close() }
}
