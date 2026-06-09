// Package statusled manages LED feedback states so users can see what Lamp is doing.
// States have priority: connectivity > error > ota > booting > hal_down > agent_down > hardware.
// All effect writes are transient (don't clobber user's saved LED state).
// When a state clears, the strip is restored to user state — ambient resumes if no user state.
package statusled

import (
	"log/slog"
	"sync"
	"time"

	"go.autonomous.ai/os/lib/hal"
)

// State represents a named LED status.
type State string

const (
	StateOTA          State = "ota"          // Firmware updating
	StateError        State = "error"        // System error
	StateBooting      State = "booting"      // Starting up
	StateConnectivity State = "connectivity" // No internet connection
	StateHALDown   State = "hal_down"  // HAL hardware server unreachable
	StateAgentDown    State = "agent_down"   // OpenClaw agent disconnected
	StateHardware     State = "hardware"     // Hardware component failure (servo/led/audio/voice)
)

// stateConfig defines the LED effect for each state.
type stateConfig struct {
	Effect string
	R, G, B int
	Speed  float64
}

var configs = map[State]stateConfig{
	StateOTA:          {Effect: "breathing", R: 0, G: 255, B: 0, Speed: 3.0},     // green — firmware updating
	StateError:        {Effect: "breathing", R: 255, G: 0, B: 0, Speed: 3.0},    // red — system error
	StateBooting:      {Effect: "breathing", R: 0, G: 80, B: 255, Speed: 3.0},   // blue — starting up
	StateConnectivity: {Effect: "breathing", R: 255, G: 80, B: 0, Speed: 3.0},   // orange — no internet
	StateHALDown:   {Effect: "breathing", R: 180, G: 0, B: 255, Speed: 3.0},  // purple — HAL down
	StateAgentDown:    {Effect: "breathing", R: 0, G: 200, B: 200, Speed: 3.0},  // cyan — OpenClaw disconnected
	StateHardware:     {Effect: "breathing", R: 255, G: 255, B: 0, Speed: 3.0},  // yellow — hardware component failure
}

// priority determines which state wins when multiple are active.
var priority = map[State]int{
	StateHardware:     1,
	StateAgentDown:    2,
	StateHALDown:   3,
	StateBooting:      4,
	StateOTA:          5,
	StateError:        6,
	StateConnectivity: 7,
}

// Service manages status LED states.
type Service struct {
	mu     sync.Mutex
	active map[State]bool
}

// ProvideService creates a StatusLED service.
func ProvideService() *Service {
	return &Service{
		active: make(map[State]bool),
	}
}

// Set activates a status LED state.
func (s *Service) Set(state State) {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.active[state] = true
	s.applyHighest()
	slog.Info("status LED set", "component", "statusled", "state", state)
}

// Clear deactivates a status LED state. No-op if state wasn't active.
func (s *Service) Clear(state State) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if _, was := s.active[state]; !was {
		// State already inactive — don't fire another RestoreLED.
		// Without this guard, callers that Clear unconditionally on every
		// tick (e.g. healthwatch poll) would trigger /led/restore each
		// tick, repainting the strip to off or to the user color
		// indefinitely.
		return
	}
	delete(s.active, state)

	if len(s.active) == 0 {
		hal.RestoreLED()
		slog.Info("status LED cleared", "component", "statusled", "state", state)
		return
	}
	// Another state still active — show it
	s.applyHighest()
	slog.Info("status LED cleared, showing next", "component", "statusled", "cleared", state)
}

// applyHighest applies the LED effect for the highest-priority active state.
// Must be called with s.mu held.
func (s *Service) applyHighest() {
	var best State
	bestPri := 0
	for st := range s.active {
		if p := priority[st]; p > bestPri {
			bestPri = p
			best = st
		}
	}
	if cfg, ok := configs[best]; ok {
		hal.SetEffect(cfg.Effect, cfg.R, cfg.G, cfg.B, cfg.Speed)
	}
}

// FlashReady fires a brief white flash to indicate the agent is ready/listening.
// No-ops if a status state is already active (avoids interrupting error/processing indicators).
// After 1s the flash stops and ambient resumes.
func (s *Service) FlashReady() {
	s.mu.Lock()
	if len(s.active) > 0 {
		s.mu.Unlock()
		return
	}
	hal.SetEffect("notification_flash", 255, 255, 255, 1.0)
	s.mu.Unlock()
	slog.Info("status LED ready flash", "component", "statusled")
	go func() {
		time.Sleep(time.Second)
		s.mu.Lock()
		defer s.mu.Unlock()
		if len(s.active) == 0 {
			hal.RestoreLED()
		}
	}()
}
