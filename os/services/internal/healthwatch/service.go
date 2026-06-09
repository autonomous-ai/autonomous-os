// Package healthwatch monitors HAL component health and auto-recovers
// from ALSA microphone failures that can cause SIGABRT crashes.
//
// Root cause context: When the ALSA mic stream (PaAlsaStreamComponent_Initialize)
// fails continuously, starting a heavy servo animation like happy_wiggle can
// trigger a double fault → SIGABRT in the HAL Python process.
// This watcher detects sensing degradation early and restarts the voice
// pipeline before that happens.
//
// Guard against false positives during HAL restart:
// Recovery is only triggered when voice was previously confirmed healthy
// (voice: true) and then sensing goes false. This prevents premature
// restarts during normal HAL startup or systemd-triggered restarts.
package healthwatch

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/monitor"
	"go.autonomous.ai/os/internal/statusled"
	"go.autonomous.ai/os/lib/hal"
	"go.autonomous.ai/os/lib/i18n"
	"go.autonomous.ai/os/server/config"
)

const (
	pollInterval    = 5 * time.Second
	failThreshold   = 2 // consecutive sensing failures before acting
	restartCooldown = 30 * time.Second
)

// Service polls HAL /health and auto-restarts the voice pipeline
// when ALSA/sensing failures are detected.
type Service struct {
	bus       *monitor.Bus
	cfg       *config.Config
	statusLED *statusled.Service
}

// ProvideService constructs a HealthWatchService.
func ProvideService(bus *monitor.Bus, cfg *config.Config, sled *statusled.Service) *Service {
	return &Service{
		bus:       bus,
		cfg:       cfg,
		statusLED: sled,
	}
}

// Start begins the health polling loop. Blocks until ctx is cancelled.
func (s *Service) Start(ctx context.Context) {
	slog.Info("starting health watcher", "component", "healthwatch")

	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()

	consecutiveFails := 0
	var lastRestart time.Time
	// voiceWasRunning is true once we confirm voice:true from HAL.
	// Recovery is only triggered after voice was running — this prevents
	// false positives when HAL just restarted (systemd or cold boot)
	// and voice hasn't been started yet by Lamp.
	voiceWasRunning := false
	// wasUnreachable tracks HAL downtime so we can announce recovery via TTS.
	wasUnreachable := false

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			h, err := hal.GetHealth()
			if err != nil {
				// HAL is down (crash / systemd restart in progress).
				// Don't touch consecutiveFails — HAL being unreachable
				// is not an ALSA error. When it comes back, voiceWasRunning
				// stays true so we can still detect ALSA re-failure after
				// restart if it happens again.
				slog.Debug("HAL unreachable", "component", "healthwatch", "error", err)
				s.statusLED.Set(statusled.StateHALDown)
				wasUnreachable = true
				continue
			}

			// Track when voice is first confirmed running.
			if h.Voice && !voiceWasRunning {
				slog.Info("voice pipeline confirmed running", "component", "healthwatch")
				voiceWasRunning = true
			}

			// HAL recovered from downtime — flash purple then clear.
			if wasUnreachable {
				s.statusLED.Set(statusled.StateHALDown) // now HAL is up, purple actually shows
				go func() {
					time.Sleep(3 * time.Second)
					s.statusLED.Clear(statusled.StateHALDown)
				}()
			} else {
				s.statusLED.Clear(statusled.StateHALDown)
			}

			// Hardware component check — servo/led/audio/voice.
			// Camera and sensing excluded (may be off by scene preset).
			servoOK := h.Servo
			if ss, err := hal.GetServoStatus(); err == nil {
				for name, info := range ss.Servos {
					if !info.Online {
						servoOK = false
						slog.Warn("servo offline", "component", "healthwatch", "servo", name, "id", info.ID)
					}
				}
			}
			if servoOK && h.LED && h.Audio && h.Voice {
				s.statusLED.Clear(statusled.StateHardware)
			} else {
				s.statusLED.Set(statusled.StateHardware)
				slog.Warn("hardware component failure", "component", "healthwatch",
					"servo", servoOK, "led", h.LED, "audio", h.Audio, "voice", h.Voice)
			}

			// Announce via TTS once voice+TTS are ready.
			if wasUnreachable && h.Voice && h.TTS {
				wasUnreachable = false
				slog.Info("HAL recovered from downtime, announcing via TTS", "component", "healthwatch")
				go s.speakRecovery()
			} else if wasUnreachable && (h.Voice || h.TTS) {
				// Still waiting for both voice and TTS to be ready
			} else {
				wasUnreachable = false
			}

			if h.Sensing {
				// Sensing is healthy — reset failure counter.
				if consecutiveFails >= failThreshold {
					slog.Info("ALSA/sensing recovered", "component", "healthwatch")
					s.bus.Push(domain.MonitorEvent{
						Type:    "hw_alsa_recover",
						Summary: "ALSA mic stream recovered",
					})
				}
				consecutiveFails = 0
				continue
			}

			// sensing: false — but only act if voice was running before.
			// If voice was never running (e.g. HAL just restarted and
			// Lamp hasn't called /voice/start yet), sensing=false is expected
			// and we should not interfere.
			if !voiceWasRunning {
				slog.Debug("sensing false but voice never ran — skipping (HAL startup?)", "component", "healthwatch")
				continue
			}

			consecutiveFails++
			slog.Warn("ALSA/sensing degraded",
				"component", "healthwatch",
				"consecutiveFails", consecutiveFails,
				"sensing", h.Sensing,
				"audio", h.Audio,
				"voice", h.Voice,
			)

			if consecutiveFails < failThreshold {
				continue
			}

			// Threshold reached — emit event visible in Flow Monitor.
			s.bus.Push(domain.MonitorEvent{
				Type:    "hw_alsa_error",
				Summary: fmt.Sprintf("ALSA mic stream failing (%d consecutive)", consecutiveFails),
				Detail: map[string]any{
					"sensing": h.Sensing,
					"audio":   h.Audio,
					"voice":   h.Voice,
				},
			})

			// Cooldown to avoid restart storms.
			if time.Since(lastRestart) < restartCooldown {
				slog.Debug("skipping voice restart — within cooldown", "component", "healthwatch")
				continue
			}

			s.restartVoice()
			lastRestart = time.Now()
			// Reset so we don't keep restarting on every poll after cooldown.
			// voiceWasRunning stays true — we'll re-confirm after restart.
			voiceWasRunning = false
		}
	}
}

// speakRecovery announces over TTS that the lamp is back after a HAL
// downtime. Phrase pool lives in lib/i18n (PhraseRecovery).
func (s *Service) speakRecovery() {
	phrase := i18n.Pick(i18n.PhraseRecovery)
	if err := hal.Speak(phrase); err != nil {
		slog.Warn("recovery TTS failed", "component", "healthwatch", "error", err)
		return
	}
	slog.Info("recovery TTS sent", "component", "healthwatch")
}

// restartVoice stops the HAL voice pipeline and restarts it.
// This clears the stuck ALSA stream state before it can cause a SIGABRT.
//
// HAL picks its STT provider at start time:
//   - Deepgram if deepgram_api_key is set
//   - AutonomousSTT (llm_api_key + llm_base_url) as fallback
//
// We always send all three keys so HAL can choose.
func (s *Service) restartVoice() {
	slog.Info("restarting HAL voice pipeline to recover ALSA", "component", "healthwatch")

	// Stop first — ignore errors (pipeline may already be stopped).
	_ = hal.StopVoicePipeline()

	time.Sleep(2 * time.Second)

	// Always attempt restart — HAL falls back to AutonomousSTT if no Deepgram key.
	if err := hal.StartVoice(hal.VoiceStartConfig{
		DeepgramKey: s.cfg.DeepgramAPIKey,
		LLMKey:      s.cfg.LLMAPIKey,
		LLMBaseURL:  s.cfg.LLMBaseURL,
		TTSProvider: s.cfg.TTSProvider,
	}); err != nil {
		slog.Error("voice restart failed", "component", "healthwatch", "error", err)
		s.bus.Push(domain.MonitorEvent{
			Type:    "hw_alsa_restart_failed",
			Summary: "voice pipeline restart failed: " + err.Error(),
		})
		return
	}

	slog.Info("HAL voice pipeline restarted", "component", "healthwatch")
	s.bus.Push(domain.MonitorEvent{
		Type:    "hw_alsa_restarted",
		Summary: "voice pipeline restarted to clear ALSA failure",
	})
}
