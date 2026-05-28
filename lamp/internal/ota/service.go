package ota

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/internal/network"
	"go-lamp.autonomous.ai/server/config"
)

const defaultPollInterval = 1 * time.Hour

const noInternetRecheckInterval = 30 * time.Second

// Status is the OTA status exposed for the API.
type Status struct {
	CurrentVersion   string              `json:"current_version"`
	AvailableVersion string              `json:"available_version"`
	UpdateURL        string              `json:"update_url"`
	UpdateAvailable  bool                `json:"update_available"`
	OpenClaw         domain.OTAComponent `json:"openclaw"`
	Web              domain.OTAComponent `json:"web"`
}

// Service polls the OTA metadata URL at a configurable interval and exposes update status.
type Service struct {
	cfg     *config.Config
	network *network.Service
	client  *http.Client

	mu       sync.RWMutex
	metadata *domain.OTAMetadata
	status   Status
}

// ProvideService creates a new OTA service.
func ProvideService(cfg *config.Config, netSvc *network.Service) *Service {
	return &Service{
		cfg:     cfg,
		network: netSvc,
		client: &http.Client{
			Timeout: 15 * time.Second,
		},
		status: Status{
			CurrentVersion: config.LumiVersion,
		},
	}
}

// Start runs the OTA poller: when OTAMetadataURL is set and there is internet, polls at OTAPollInterval.
// On config change, reloads config. Stops when ctx is cancelled.
func (s *Service) Start(ctx context.Context, configChanged chan bool) {
	if configChanged == nil {
		configChanged = make(chan bool)
	}

recheck:
	for {
		s.mu.RLock()
		otaURL := s.cfg.OTAMetadataURL
		s.mu.RUnlock()
		if otaURL == "" {
			select {
			case <-ctx.Done():
				return
			case <-configChanged:
			}
			continue
		}

		ok, err := s.network.CheckInternet()
		if err != nil || !ok {
			select {
			case <-ctx.Done():
				return
			case <-configChanged:
			case <-time.After(noInternetRecheckInterval):
			}
			continue recheck
		}

		interval := s.parsePollInterval()
		ticker := time.NewTicker(interval)
		s.poll()
		for {
			select {
			case <-ctx.Done():
				ticker.Stop()
				return
			case <-configChanged:
				ticker.Stop()
				continue recheck
			case <-ticker.C:
				s.poll()
			}
		}
	}
}

func (s *Service) parsePollInterval() time.Duration {
	s.mu.RLock()
	raw := s.cfg.OTAPollInterval
	s.mu.RUnlock()
	if raw == "" {
		return defaultPollInterval
	}
	d, err := time.ParseDuration(raw)
	if err != nil {
		return defaultPollInterval
	}
	if d < time.Minute {
		return time.Minute
	}
	return d
}

func (s *Service) poll() {
	s.mu.RLock()
	url := s.cfg.OTAMetadataURL
	s.mu.RUnlock()
	if url == "" {
		return
	}

	ok, err := s.network.CheckInternet()
	if err != nil || !ok {
		return
	}

	resp, err := s.client.Get(url)
	if err != nil {
		slog.Error("fetch metadata failed", "component", "ota", "url", url, "error", err)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		slog.Error("metadata returned non-OK status", "component", "ota", "url", url, "status", resp.StatusCode)
		return
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		slog.Error("read body failed", "component", "ota", "error", err)
		return
	}

	var meta domain.OTAMetadata
	if err := json.Unmarshal(body, &meta); err != nil {
		slog.Error("unmarshal body failed", "component", "ota", "error", err)
		return
	}

	lampMeta := meta[domain.OTAKeyLamp]
	openclawMeta := meta[domain.OTAKeyOpenClaw]
	webMeta := meta[domain.OTAKeyWeb]

	s.mu.Lock()
	s.metadata = &meta
	current := config.LumiVersion
	available := lampMeta.Version
	s.status = Status{
		CurrentVersion:   current,
		AvailableVersion: available,
		UpdateURL:        lampMeta.URL,
		UpdateAvailable:  available != "" && available != current,
		OpenClaw:         openclawMeta,
		Web:              webMeta,
	}
	s.mu.Unlock()

	slog.Info("metadata fetched", "component", "ota", "current", current, "available", available, "updateAvailable", s.status.UpdateAvailable)
}

// GetStatus returns the current OTA status for the API.
func (s *Service) GetStatus() Status {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.status
}
