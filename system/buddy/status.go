package buddy

import (
	"time"

	"github.com/gorilla/websocket"
)

// Status is a public snapshot. It deliberately excludes tokens and pairing codes.
// Revision is monotonic only within InstanceID, which changes on service restart.
type Status struct {
	Paired     bool       `json:"paired"`
	Connected  bool       `json:"connected"`
	InstanceID string     `json:"instance_id"`
	Revision   uint64     `json:"revision"`
	BuddyID    string     `json:"buddy_id,omitempty"`
	Name       string     `json:"name,omitempty"`
	OSVersion  string     `json:"os_version,omitempty"`
	PairedAt   *time.Time `json:"paired_at,omitempty"`
}

func (s *Service) Status() Status {
	s.statusMu.Lock()
	defer s.statusMu.Unlock()
	status := Status{InstanceID: s.instanceID, Revision: s.revision}
	if record := s.store.Get(); record != nil {
		status.Paired = true
		status.Connected = s.registry.Conn() != nil
		status.BuddyID = record.BuddyID
		status.Name = record.Name
		status.OSVersion = record.OSVersion
		status.PairedAt = &record.PairedAt
	}
	return status
}

// StatusChanges is a single-consumer wakeup queue, not an event history.
// Slow publishers coalesce transitions and read the latest snapshot via Status.
func (s *Service) StatusChanges() <-chan struct{} { return s.statusChanges }

// statusChangedLocked never waits for network I/O; the MQTT worker owns delivery.
func (s *Service) statusChangedLocked() {
	s.revision++
	select {
	case s.statusChanges <- struct{}{}:
	default:
	}
}

func (s *Service) clearConnection(conn *websocket.Conn) {
	s.statusMu.Lock()
	defer s.statusMu.Unlock()
	if s.registry.Conn() == conn {
		s.registry.ClearConnection(conn)
		s.statusChangedLocked()
	}
}
