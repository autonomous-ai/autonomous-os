package buddy

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/gorilla/websocket"
)

// Service is the top-level coordinator for the buddy feature. Composed of a
// pairing-code generator, a persistent pairing store, an in-memory connection
// registry, and a dispatcher.
type Service struct {
	store      *Store
	pairing    *PairingCodeStore
	registry   *Registry
	dispatcher *Dispatcher
}

// ProvideService wires the buddy subsystem. It loads any existing pairing from
// disk so a previously-paired buddy can reconnect after a lamp restart.
func ProvideService() (*Service, error) {
	store := NewStore(BuddiesFilePath)
	if err := store.Load(); err != nil {
		return nil, fmt.Errorf("load buddy store: %w", err)
	}
	pairing := NewPairingCodeStore(60 * time.Second)
	registry := NewRegistry()
	dispatcher := NewDispatcher(registry)
	return &Service{
		store:      store,
		pairing:    pairing,
		registry:   registry,
		dispatcher: dispatcher,
	}, nil
}

// IssuePairingCode generates a fresh 6-digit code valid for 60s, invalidating any prior code.
func (s *Service) IssuePairingCode() (string, time.Duration) {
	return s.pairing.Issue()
}

// ConfirmPairing validates a submitted code and persists a new pairing record,
// returning the long-lived token + buddy ID for the buddy to use.
func (s *Service) ConfirmPairing(name, fingerprint, osVersion, code string) (*PairingRecord, error) {
	if !s.pairing.Consume(code) {
		return nil, fmt.Errorf("invalid or expired code")
	}
	record := &PairingRecord{
		BuddyID:     newBuddyID(),
		Token:       newToken(),
		Name:        name,
		Fingerprint: fingerprint,
		OSVersion:   osVersion,
		PairedAt:    time.Now().UTC(),
	}
	if err := s.store.Set(record); err != nil {
		return nil, fmt.Errorf("save pairing: %w", err)
	}
	slog.Info("buddy paired", "component", "buddy", "id", record.BuddyID, "name", name, "os", osVersion)
	return record, nil
}

// Unpair drops the current buddy: closes the WS, clears the registry, removes the on-disk record.
func (s *Service) Unpair() error {
	if conn := s.registry.Conn(); conn != nil {
		_ = conn.Close()
	}
	s.registry.Clear()
	if err := s.store.Clear(); err != nil {
		return fmt.Errorf("clear store: %w", err)
	}
	slog.Info("buddy unpaired", "component", "buddy")
	return nil
}

// Paired returns the current paired record (snapshot) or nil.
func (s *Service) Paired() *PairingRecord {
	return s.store.Get()
}

// ValidateToken returns the record matching the bearer token, or nil.
func (s *Service) ValidateToken(token string) *PairingRecord {
	return s.store.ByToken(token)
}

// RegisterConnection installs the buddy's WS for command dispatch.
func (s *Service) RegisterConnection(conn *websocket.Conn) {
	s.registry.Set(conn)
}

// Connected reports whether a buddy is currently online.
func (s *Service) Connected() bool {
	return s.registry.Conn() != nil
}

// Dispatch sends a command to the connected buddy and waits for its response.
func (s *Service) Dispatch(ctx context.Context, cmd Command) (json.RawMessage, error) {
	return s.dispatcher.Dispatch(ctx, cmd)
}
