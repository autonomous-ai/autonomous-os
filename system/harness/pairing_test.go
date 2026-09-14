package harness

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func waitPairState(t *testing.T, s *Service, state string) PairInfo {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		info := s.PairStatus()
		if info.State == state {
			return info
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("pairing did not reach %s: %#v", state, s.PairStatus())
	return PairInfo{}
}
func testDirectSocket(t *testing.T, s *Service) *websocket.Conn {
	t.Helper()
	server := httptest.NewServer(s)
	t.Cleanup(server.Close)
	ws, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http")+"/api/harness/ws", nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ws.Close() })
	return ws
}
func TestDeviceGeneratesCodeAndCancellationClearsIt(t *testing.T) {
	s, err := NewService(t.TempDir(), Callbacks{})
	if err != nil {
		t.Fatal(err)
	}
	info, err := s.StartPair(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.CancelPair() })
	if len(info.Code) != 6 || !info.Pairing || info.State != "waiting" {
		t.Fatal("missing device-generated code")
	}
	for _, r := range info.Code {
		if !strings.ContainsRune("0123456789ABCDEFGHJKMNPQRSTVWXYZ", r) {
			t.Fatal("invalid code alphabet")
		}
	}
	if info.ExpiresAt < time.Now().Add(59*time.Second).UnixMilli() {
		t.Fatal("pairing lifetime is not sixty seconds")
	}
	ws := testDirectSocket(t, s)
	if err = ws.WriteJSON(Frame{"type": "machine_select", "payload": Frame{"machineId": "test-machine", "label": "Test Mac"}}); err != nil {
		t.Fatal(err)
	}
	_ = ws.SetReadDeadline(time.Now().Add(time.Second))
	var selected, intent Frame
	if err = ws.ReadJSON(&selected); err != nil {
		t.Fatal(err)
	}
	if stringField(selected, "type") != "machine_selected" {
		t.Fatal("missing machine selection")
	}
	if err = ws.ReadJSON(&intent); err != nil {
		t.Fatal(err)
	}
	if stringField(intent, "type") != "e2e_pair_intent" || payloadOf(intent)["role"] != "device" {
		t.Fatal("not original device pairing")
	}
	if payloadOf(intent)["code"] != nil {
		t.Fatal("pairing code transmitted")
	}
	raw, err := os.ReadFile(s.path)
	if err != nil {
		t.Fatal(err)
	}
	var saved Frame
	if err = json.Unmarshal(raw, &saved); err != nil {
		t.Fatal(err)
	}
	if saved["code"] != nil || saved["peer"] != nil {
		t.Fatal("unauthenticated trust persisted")
	}
	if _, err = s.StartPair(context.Background()); err == nil {
		t.Fatal("parallel pairing permitted")
	}
	if err = s.CancelPair(); err != nil {
		t.Fatal(err)
	}
	cancelled := s.PairStatus()
	if cancelled.Code != "" || cancelled.Pairing || cancelled.State != "cancelled" {
		t.Fatal("cancel retained code")
	}
	if _, _, err = ws.ReadMessage(); err == nil {
		t.Fatal("cancel did not close socket")
	}
	if s.Status().Paired {
		t.Fatal("cancel saved trust")
	}
}
func TestPairDeadlineClearsDisplayedCode(t *testing.T) {
	s, err := NewService(t.TempDir(), Callbacks{})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if _, err = s.StartPair(ctx); err != nil {
		t.Fatal(err)
	}
	info := waitPairState(t, s, "expired")
	if info.Code != "" || info.Pairing {
		t.Fatal("expired code remained visible")
	}
}
func TestExistingPeerCannotBeReplacedByStartingPair(t *testing.T) {
	s, err := NewService(t.TempDir(), Callbacks{})
	if err != nil {
		t.Fatal(err)
	}
	incumbent := &peer{MachineID: "existing-machine", Protocol: "harness-device-direct-v1"}
	s.disk.Peer = incumbent
	if _, err = s.StartPair(context.Background()); err == nil {
		t.Fatal("replaced incumbent")
	}
	if s.disk.Peer != incumbent {
		t.Fatal("trust changed")
	}
}
func TestClosedPairingRejectsUntrustedComputer(t *testing.T) {
	s, err := NewService(t.TempDir(), Callbacks{})
	if err != nil {
		t.Fatal(err)
	}
	ws := testDirectSocket(t, s)
	_ = ws.WriteJSON(Frame{"type": "machine_select", "payload": Frame{"machineId": "unknown", "label": "Untrusted"}})
	_ = ws.SetReadDeadline(time.Now().Add(time.Second))
	if _, _, err = ws.ReadMessage(); err == nil {
		t.Fatal("accepted untrusted connection while pairing closed")
	}
	if s.disk.Peer != nil {
		t.Fatal("untrusted identity saved")
	}
}

func TestExpiredProvisionalPeerDoesNotBlockNewPairWindow(t *testing.T) {
	s, err := NewService(t.TempDir(), Callbacks{})
	if err != nil {
		t.Fatal(err)
	}
	s.disk.Peer = &peer{MachineID: "expired", Protocol: "harness-device-direct-v1", ProvisionalUntil: time.Now().Add(-time.Second).UnixMilli()}
	if err = s.saveLocked(); err != nil {
		t.Fatal(err)
	}
	info, err := s.StartPair(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer s.CancelPair()
	if !info.Pairing || s.disk.Peer != nil {
		t.Fatal("expired peer still blocks pairing")
	}
}

func TestStatusExpiresProvisionalPeerAndReportsPersistenceFailure(t *testing.T) {
	s, err := NewService(t.TempDir(), Callbacks{})
	if err != nil {
		t.Fatal(err)
	}
	s.disk.Peer = &peer{MachineID: "expired", Protocol: "harness-device-direct-v1", ProvisionalUntil: time.Now().Add(-time.Second).UnixMilli()}
	if err = s.saveLocked(); err != nil {
		t.Fatal(err)
	}
	if err = os.Remove(s.path); err != nil {
		t.Fatal(err)
	}
	if err = os.Mkdir(s.path, 0700); err != nil {
		t.Fatal(err)
	}
	failed := s.Status()
	if failed.Error == "" || failed.MachineID != "expired" || s.disk.Peer == nil {
		t.Fatal("failed expiry silently discarded trust")
	}
	if err = os.Remove(s.path); err != nil {
		t.Fatal(err)
	}
	expired := s.Status()
	if expired.Error != "" || expired.MachineID != "" || s.disk.Peer != nil {
		t.Fatal("status did not clean up expired trust")
	}
}

func TestTrustRemovalFailurePreservesPinAndAllowsRetry(t *testing.T) {
	for _, operation := range []string{"cancel", "unpair"} {
		t.Run(operation, func(t *testing.T) {
			dataDir := t.TempDir()
			s, err := NewService(dataDir, Callbacks{})
			if err != nil {
				t.Fatal(err)
			}
			p := &peer{MachineID: "retained-machine", Protocol: "harness-device-direct-v1", PublicKey: b64(make([]byte, 32))}
			remove := s.Unpair
			if operation == "cancel" {
				if _, err = s.StartPair(context.Background()); err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { _ = s.CancelPair() })
				s.attempt.provisional = true
				p.ProvisionalUntil = time.Now().Add(time.Minute).UnixMilli()
				remove = s.CancelPair
			}
			s.disk.Peer = p
			if err = s.saveLocked(); err != nil {
				t.Fatal(err)
			}
			// Keep the durable trust intact while making the write path invalid.
			dir := filepath.Dir(s.path)
			backup := dir + ".saved"
			if err = os.Rename(dir, backup); err != nil {
				t.Fatal(err)
			}
			if err = os.WriteFile(dir, []byte("blocked"), 0600); err != nil {
				t.Fatal(err)
			}
			if err = remove(); err == nil {
				t.Fatal("trust deletion unexpectedly succeeded")
			}
			status := s.Status()
			if s.disk.Peer != p || status.MachineID != p.MachineID || status.State != "disconnected" || status.Error == "" || status.Connected || status.Pairing {
				t.Fatalf("failed removal hid retained trust: %#v", status)
			}
			if operation == "unpair" && !status.Paired {
				t.Fatal("failed unpair hid the durable pairing")
			}
			if err = os.Remove(dir); err != nil {
				t.Fatal(err)
			}
			if err = os.Rename(backup, dir); err != nil {
				t.Fatal(err)
			}
			reloaded, err := NewService(dataDir, Callbacks{})
			if err != nil || reloaded.Status().MachineID != status.MachineID {
				t.Fatalf("memory and durable trust disagree: %v", err)
			}
			// A cancelled provisional pair can be removed through Unpair.
			if err = s.Unpair(); err != nil {
				t.Fatal(err)
			}
			reloaded, err = NewService(dataDir, Callbacks{})
			if err != nil || reloaded.Status().MachineID != "" || s.Status().Error != "" {
				t.Fatalf("trust deletion retry failed: %v", err)
			}
		})
	}
}
