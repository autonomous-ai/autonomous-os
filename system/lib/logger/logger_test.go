package logger

import (
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestLevelFromEnv(t *testing.T) {
	tests := []struct {
		name  string
		value string
		want  slog.Level
	}{
		{name: "debug", value: "DEBUG", want: slog.LevelDebug},
		{name: "case and whitespace ignored", value: " warning ", want: slog.LevelWarn},
		{name: "error", value: "ERROR", want: slog.LevelError},
		{name: "invalid uses default", value: "TRACE", want: slog.LevelInfo},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("HAL_LOG_LEVEL", tt.value)
			if got := levelFromEnv(); got != tt.want {
				t.Fatalf("levelFromEnv() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestGELFHandlerDropsWhenBoundedQueueIsFull(t *testing.T) {
	started := make(chan struct{}, 1)
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case started <- struct{}{}:
		default:
		}
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}))
	defer server.Close()

	oldURL, oldUsername, oldPassword := gelfURL, gelfUsername, gelfPassword
	gelfURL, gelfUsername, gelfPassword = server.URL, "", ""
	defer func() { gelfURL, gelfUsername, gelfPassword = oldURL, oldUsername, oldPassword }()

	h := newGELFHandler(slog.LevelInfo, "test")
	defer h.sender.close()

	if err := h.Handle(context.Background(), slog.NewRecord(time.Now(), slog.LevelInfo, "first", 0)); err != nil {
		t.Fatalf("first Handle() error = %v", err)
	}
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("GELF worker did not start request")
	}

	for i := 0; i <= gelfQueueSize; i++ {
		if err := h.Handle(context.Background(), slog.NewRecord(time.Now(), slog.LevelInfo, "queued", 0)); err != nil {
			t.Fatalf("Handle() error = %v", err)
		}
	}
	if h.sender.dropped.Load() == 0 {
		t.Fatal("expected at least one GELF record to be dropped when queue is full")
	}

	start := time.Now()
	if err := h.Handle(context.Background(), slog.NewRecord(time.Now(), slog.LevelInfo, "overflow", 0)); err != nil {
		t.Fatalf("overflow Handle() error = %v", err)
	}
	if elapsed := time.Since(start); elapsed > 100*time.Millisecond {
		t.Fatalf("overflow Handle() blocked for %s", elapsed)
	}
	close(release)
}

func TestGELFSenderFlushesQueuedRecordsOnClose(t *testing.T) {
	var received atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received.Add(1)
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()

	oldURL, oldUsername, oldPassword := gelfURL, gelfUsername, gelfPassword
	gelfURL, gelfUsername, gelfPassword = server.URL, "", ""
	defer func() { gelfURL, gelfUsername, gelfPassword = oldURL, oldUsername, oldPassword }()

	h := newGELFHandler(slog.LevelInfo, "test")
	for i := 0; i < 3; i++ {
		if err := h.Handle(context.Background(), slog.NewRecord(time.Now(), slog.LevelInfo, "flush", 0)); err != nil {
			t.Fatalf("Handle() error = %v", err)
		}
	}
	h.sender.close()

	if got := received.Load(); got != 3 {
		t.Fatalf("GELF records sent before close = %d, want 3", got)
	}
}
