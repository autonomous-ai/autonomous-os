package environment

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"reflect"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

type Service struct {
	Startup   *StartupCoordinator
	Settings  func() config.EnvironmentConfig
	Available func() bool
	Read      func(context.Context) (json.RawMessage, error)
	Send      func(context.Context, Event) (bool, error)
}

// Run polls locally and dispatches an optional startup snapshot or sustained changes.
// Settings are re-read on each tick, independently of the shared config channel.
func (s Service) Run(ctx context.Context) {
	var detector *Detector
	var current config.EnvironmentConfig
	timer := time.NewTimer(0)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}
		settings := s.Settings()
		if err := settings.Validate(); err != nil {
			slog.Warn("invalid environment config", "component", "environment", "error", err)
			detector = nil
			if s.Startup != nil {
				s.Startup.update(false, nil, nil)
			}
			timer.Reset(10 * time.Second)
			continue
		}
		if detector == nil || !reflect.DeepEqual(current, settings) {
			current = settings.Clone()
			detector = NewDetector(current)
		}
		if s.Startup != nil {
			if acknowledged := s.Startup.takeAcknowledged(); acknowledged != nil {
				detector.Attempted(time.Now(), acknowledged, true)
			}
		}
		if settings.Enabled && s.Available() {
			body, err := s.Read(ctx)
			var snapshot Snapshot
			if err == nil {
				err = json.Unmarshal(body, &snapshot)
			}
			if err != nil {
				detector.ResetReadings()
				if s.Startup != nil {
					s.Startup.update(settings.InitialReport, nil, nil)
				}
				slog.Debug("environment sample unavailable", "component", "environment", "error", err)
			} else {
				now := time.Now()
				event := detector.Observe(now, snapshot)
				if s.Startup != nil {
					initial, expires := detector.startupSnapshot(now, snapshot)
					s.Startup.update(settings.InitialReport, initial, expires)
					if pending, next := s.Startup.initial(now, settings.RetryIntervalS); pending {
						event = next
					}
				}
				if event != nil {
					accepted, err := s.Send(ctx, *event)
					detector.Attempted(time.Now(), event, accepted)
					if accepted && event.Reason == "initial" && s.Startup != nil {
						s.Startup.acceptedInitial()
					}
					if err != nil {
						slog.Warn("environment dispatch failed", "component", "environment", "error", err)
					}
				}
			}
		} else {
			detector.ResetReadings()
			if s.Startup != nil {
				s.Startup.update(false, nil, nil)
			}
		}
		timer.Reset(time.Duration(settings.EvaluateIntervalS * float64(time.Second)))
	}
}

// HTTPSender uses the same local sensing endpoint as camera/microphone events.
// A queued acknowledgment is best-effort: replay can later expire or be dropped.
func HTTPSender(url string) func(context.Context, Event) (bool, error) {
	client := &http.Client{Timeout: 30 * time.Second}
	return func(ctx context.Context, event Event) (bool, error) {
		body, err := json.Marshal(map[string]string{"type": "environment.update", "message": event.Message()})
		if err != nil {
			return false, err
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
		if err != nil {
			return false, err
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			return false, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return false, fmt.Errorf("sensing event HTTP %d", resp.StatusCode)
		}
		var reply struct {
			Status int `json:"status"`
			Data   struct {
				RunID   string `json:"runId"`
				Handler string `json:"handler"`
			} `json:"data"`
		}
		if err := json.NewDecoder(io.LimitReader(resp.Body, 65536)).Decode(&reply); err != nil {
			return false, err
		}
		return reply.Status == 1 && (reply.Data.RunID != "" || reply.Data.Handler == "queued"), nil
	}
}
