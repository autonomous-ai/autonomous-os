package schedule

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// Intent is one device-originated schedule change the user made locally, held
// until the backend confirms it.
//
// WHY A SEPARATE FILE FROM schedules.json: schedules.json holds only what the
// backend has confirmed, and the runner fires from schedules.json alone. That
// is what makes "a task never runs before the cloud knows about it" a
// structural property rather than a flag someone can forget to check — an
// unconfirmed task is not merely marked un-runnable, it is not in the file the
// runner reads at all.
type Intent struct {
	// IntentID is the idempotency key. Generated ONCE per user action and kept
	// across every retry, so the backend's ledger can collapse the replays that
	// a reconnect inevitably produces into a single applied mutation.
	IntentID string `json:"intent_id"`

	Op         string `json:"op"`                    // "create" | "update" | "delete"
	ScheduleID string `json:"schedule_id,omitempty"` // Target; empty for create

	// BaseRev is the rev the user's edit was based on — the compare half of the
	// backend's compare-and-swap. Meaningless for create.
	BaseRev uint64 `json:"base_rev,omitempty"`

	// Payload is the proposed row for create/update; nil for delete.
	Payload *IntentPayload `json:"schedule,omitempty"`

	CreatedAt  time.Time `json:"created_at"`
	LastSentAt time.Time `json:"last_sent_at,omitempty"`
	Attempts   int       `json:"attempts,omitempty"`
}

// IntentPayload is the user-editable subset of a schedule. Deliberately not a
// whole Schedule: id, rev, status and all run bookkeeping are backend-owned, so
// they are absent here and the device has no way to propose them.
type IntentPayload struct {
	Name         string `json:"name"`
	Instructions string `json:"instructions"`
	Enabled      bool   `json:"enabled"`

	// Kind is "agent" or "speak" — see Schedule.Kind. It MUST be carried here
	// even though the local UI has no control for it yet, because an UPDATE
	// proposal sends the whole mutable subset and the backend writes every
	// field of it. Omit this and editing a speak task's NAME on the device
	// would silently demote it to an agent task, which then reads the user's
	// sentence aloud as a prompt instead of saying it.
	//
	// Empty is "agent" here exactly as everywhere else, so a device echoing
	// back a pre-kind row it synced as "" is lossless rather than a downgrade.
	Kind string `json:"kind,omitempty"`

	Timezone     string     `json:"timezone,omitempty"`
	TemplateCode string     `json:"template_code,omitempty"`
	Cadence      Spec       `json:"schedule"`
	EndAt        *time.Time `json:"end_at,omitempty"`
}

// NewIntentID returns a fresh random idempotency key. Not a UUID library —
// 16 random bytes hex-encoded is the same 128 bits and keeps this package
// dependency-free, which matters on a device image.
func NewIntentID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("generate intent id: %w", err)
	}
	return hex.EncodeToString(b[:]), nil
}

// intentFile is the on-disk shape of intents.json.
type intentFile struct {
	Intents []Intent `json:"intents"`
}

// IntentStore persists pending intents to intents.json, a sibling of
// schedules.json. Same atomic tmp+rename discipline as Store, and the same
// tolerance for a missing or corrupt file: an unreadable queue degrades to
// empty rather than blocking the device, because the backend remains
// authoritative and the next schedule.sync repairs local state regardless.
type IntentStore struct {
	mu   sync.Mutex
	path string
}

func NewIntentStore(path string) *IntentStore {
	return &IntentStore{path: path}
}

func (s *IntentStore) loadLocked() intentFile {
	var f intentFile
	raw, err := os.ReadFile(s.path)
	if err != nil {
		return intentFile{}
	}
	if err := json.Unmarshal(raw, &f); err != nil {
		return intentFile{}
	}
	return f
}

func (s *IntentStore) saveLocked(f intentFile) error {
	if f.Intents == nil {
		f.Intents = []Intent{}
	}
	raw, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal intents: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return fmt.Errorf("create intents dir: %w", err)
	}
	tmp, err := os.CreateTemp(filepath.Dir(s.path), ".intents-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp intents: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)

	if _, err := tmp.Write(raw); err != nil {
		tmp.Close()
		return fmt.Errorf("write temp intents: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return fmt.Errorf("sync temp intents: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp intents: %w", err)
	}
	if err := os.Chmod(tmpName, 0o600); err != nil {
		return fmt.Errorf("chmod temp intents: %w", err)
	}
	if err := os.Rename(tmpName, s.path); err != nil {
		return fmt.Errorf("rename intents: %w", err)
	}
	return nil
}

// Append queues one intent.
func (s *IntentStore) Append(in Intent) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadLocked()
	f.Intents = append(f.Intents, in)
	return s.saveLocked(f)
}

// List returns the queue in submission order.
func (s *IntentStore) List() []Intent {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadLocked()
	out := make([]Intent, len(f.Intents))
	copy(out, f.Intents)
	return out
}

// Remove drops one intent by id. Called when the backend has reached a TERMINAL
// verdict on it — applied OR rejected. A rejected intent must be dropped just
// as surely as an applied one: leaving it queued would replay a proposal the
// backend has already refused on every single reconnect, forever.
func (s *IntentStore) Remove(intentID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadLocked()
	kept := f.Intents[:0]
	for _, in := range f.Intents {
		if in.IntentID != intentID {
			kept = append(kept, in)
		}
	}
	f.Intents = kept
	return s.saveLocked(f)
}

// MarkSent stamps a send attempt, for backoff and for surfacing "not syncing"
// in the UI when a device has been offline for a while.
func (s *IntentStore) MarkSent(intentID string, at time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadLocked()
	for i := range f.Intents {
		if f.Intents[i].IntentID == intentID {
			f.Intents[i].LastSentAt = at
			f.Intents[i].Attempts++
			return s.saveLocked(f)
		}
	}
	return nil
}

// ValidateIntentPayload checks a locally-authored schedule before it is queued.
//
// The backend validates too, but this runs FIRST and matters on its own: a
// proposal that fails server-side comes back as a rejection the user only sees
// after a round trip, and if the device is offline that round trip may be hours
// away. Catching it here turns "silently never happened" into an immediate,
// fixable form error.
func ValidateIntentPayload(p *IntentPayload) error {
	if p == nil {
		return fmt.Errorf("schedule is required")
	}
	if strings.TrimSpace(p.Name) == "" {
		return fmt.Errorf("name is required")
	}
	if strings.TrimSpace(p.Instructions) == "" {
		return fmt.Errorf("instructions are required")
	}
	if ResolveKind(p.Kind) == KindSpeak {
		// HAL's /voice/speak rejects anything longer and does NOT truncate, so
		// an over-long line is not "mostly spoken" — it is complete silence,
		// every time the task fires. Counted in RUNES, not bytes: the limit is
		// a character limit, and counting bytes would refuse a perfectly legal
		// line the moment it contained an accent or a non-Latin script.
		if n := utf8.RuneCountInString(p.Instructions); n > MaxSpeakChars {
			return fmt.Errorf("spoken text must be at most %d characters, got %d", MaxSpeakChars, n)
		}
	}
	return ValidateSpec(p.Cadence)
}

// ValidateSpec checks a cadence is internally consistent — that the fields
// which actually matter for the chosen repeat are present and in range.
//
// Mirrors the "repeat selects which of the remaining fields matter" rule the
// wire contract documents, so a cadence accepted here is one the runner can
// definitely compute a next fire for. Anything else would be stored, synced,
// and then silently never run.
func ValidateSpec(spec Spec) error {
	switch spec.Repeat {
	case "daily":
		return validateClockTime(spec.Time)
	case "weekly":
		if err := validateClockTime(spec.Time); err != nil {
			return err
		}
		if len(spec.Days) == 0 {
			return fmt.Errorf("weekly schedules need at least one day")
		}
		for _, d := range spec.Days {
			// 7 is accepted as an alias for Sunday, matching Spec.Days' doc.
			if d < 0 || d > 7 {
				return fmt.Errorf("day of week out of range: %d", d)
			}
		}
		return nil
	case "monthly":
		if err := validateClockTime(spec.Time); err != nil {
			return err
		}
		if spec.DayOfMonth < 1 || spec.DayOfMonth > 31 {
			return fmt.Errorf("day of month must be 1-31, got %d", spec.DayOfMonth)
		}
		return nil
	case "interval":
		if spec.EveryMs <= 0 {
			return fmt.Errorf("interval schedules need every_ms")
		}
		if time.Duration(spec.EveryMs)*time.Millisecond < minInterval {
			return fmt.Errorf("interval must be at least %s", minInterval)
		}
		return nil
	case "once":
		// The fire time travels as the schedule's EndAt-sibling At; a "once"
		// with no At can never fire.
		if spec.At == nil || spec.At.IsZero() {
			return fmt.Errorf("one-off schedules need a date and time")
		}
		return nil
	case "manual":
		return nil
	default:
		return fmt.Errorf("unsupported repeat: %q", spec.Repeat)
	}
}

// validateClockTime accepts exactly the "HH:MM" 24-hour form the runner parses.
func validateClockTime(s string) error {
	if _, err := time.Parse("15:04", s); err != nil {
		return fmt.Errorf("time must be HH:MM, got %q", s)
	}
	return nil
}
