package buddy

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// BuddiesFilePath is the on-disk JSON file for the (currently single) paired buddy.
// MVP is 1↔1 so this holds at most one record. Future multi-buddy will expand the schema.
const BuddiesFilePath = "config/buddies.json"

// PairingRecord is what the lamp persists for each paired buddy.
type PairingRecord struct {
	BuddyID     string    `json:"buddy_id"`
	Token       string    `json:"token"`
	Name        string    `json:"name"`
	Fingerprint string    `json:"fingerprint"`
	OSVersion   string    `json:"os_version"`
	PairedAt    time.Time `json:"paired_at"`
}

// storeFile is the on-disk shape: {"records":[...]}. Wrapping in an object lets us
// extend later (settings, audit log path, etc.) without a v2 schema break.
type storeFile struct {
	Records []PairingRecord `json:"records"`
}

// Store persists paired buddies to disk. Safe for concurrent use.
type Store struct {
	mu     sync.RWMutex
	path   string
	record *PairingRecord
}

func NewStore(path string) *Store {
	return &Store{path: path}
}

// Load reads the store file if it exists. Missing file is not an error.
func (s *Store) Load() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	data, err := os.ReadFile(s.path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("read %s: %w", s.path, err)
	}
	var f storeFile
	if err := json.Unmarshal(data, &f); err != nil {
		return fmt.Errorf("parse %s: %w", s.path, err)
	}
	if len(f.Records) > 0 {
		rec := f.Records[0]
		s.record = &rec
	}
	return nil
}

// Set replaces the (single) paired buddy and persists. MVP is 1↔1.
func (s *Store) Set(r *PairingRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.record = r
	return s.writeLocked()
}

// Clear removes the pairing and persists an empty store.
func (s *Store) Clear() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.record = nil
	return s.writeLocked()
}

// Get returns a snapshot of the paired record, or nil if none.
func (s *Store) Get() *PairingRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.record == nil {
		return nil
	}
	c := *s.record
	return &c
}

// ByToken returns the paired record whose token matches, or nil.
func (s *Store) ByToken(token string) *PairingRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.record == nil || s.record.Token != token {
		return nil
	}
	c := *s.record
	return &c
}

func (s *Store) writeLocked() error {
	var f storeFile
	if s.record != nil {
		f.Records = []PairingRecord{*s.record}
	}
	data, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal store: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return fmt.Errorf("create store dir: %w", err)
	}
	if err := os.WriteFile(s.path, data, 0o600); err != nil {
		return fmt.Errorf("write %s: %w", s.path, err)
	}
	return nil
}
