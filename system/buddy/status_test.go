package buddy

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"
)

func statusService(t *testing.T) *Service {
	t.Helper()
	t.Chdir(t.TempDir())
	svc, err := ProvideService()
	if err != nil {
		t.Fatal(err)
	}
	return svc
}

func statusPair(t *testing.T, svc *Service) (*PairingRecord, string) {
	t.Helper()
	code, _ := svc.IssuePairingCode()
	record, err := svc.ConfirmPairing("Test Mac", "private-fingerprint", "macOS test", code)
	if err != nil {
		t.Fatal(err)
	}
	return record, code
}

func requireStatusWakeup(t *testing.T, svc *Service) {
	t.Helper()
	select {
	case <-svc.StatusChanges():
	default:
		t.Fatal("missing status wakeup")
	}
}

func requireNoStatusWakeup(t *testing.T, svc *Service) {
	t.Helper()
	select {
	case <-svc.StatusChanges():
		t.Fatal("unexpected status wakeup")
	default:
	}
}

func TestStatusPairUnpairAndRestart(t *testing.T) {
	svc := statusService(t)
	initial := svc.Status()
	if initial.Paired || initial.Connected || initial.InstanceID == "" {
		t.Fatalf("invalid initial status: %+v", initial)
	}
	requireNoStatusWakeup(t, svc)
	record, code := statusPair(t, svc)
	requireStatusWakeup(t, svc)
	paired := svc.Status()
	if !paired.Paired || paired.Connected || paired.BuddyID != record.BuddyID || paired.Name != record.Name || paired.OSVersion != record.OSVersion || paired.PairedAt == nil || !paired.PairedAt.Equal(record.PairedAt) {
		t.Fatalf("invalid paired status: %+v", paired)
	}
	if paired.InstanceID != initial.InstanceID || paired.Revision <= initial.Revision {
		t.Fatal("pairing did not advance revision in the same instance")
	}
	raw, err := json.Marshal(paired)
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{`"token"`, `"code"`, `"fingerprint"`, record.Token, record.Fingerprint, `"` + code + `"`} {
		if strings.Contains(string(raw), secret) {
			t.Fatalf("public status contains a private value: %s", raw)
		}
	}
	restarted, err := ProvideService()
	if err != nil {
		t.Fatal(err)
	}
	restored := restarted.Status()
	if !restored.Paired || restored.Connected || restored.BuddyID != record.BuddyID || restored.InstanceID == paired.InstanceID || restored.Revision != initial.Revision {
		t.Fatalf("invalid restored status: %+v", restored)
	}
	if err := svc.Unpair(); err != nil {
		t.Fatal(err)
	}
	requireStatusWakeup(t, svc)
	unpaired := svc.Status()
	if unpaired.Paired || unpaired.Connected || unpaired.BuddyID != "" || unpaired.Name != "" || unpaired.OSVersion != "" || unpaired.PairedAt != nil || unpaired.Revision <= paired.Revision {
		t.Fatalf("invalid revoked status: %+v", unpaired)
	}
	if svc.ValidateToken(record.Token) != nil {
		t.Fatal("revoked token remains valid")
	}
	restarted, err = ProvideService()
	if err != nil {
		t.Fatal(err)
	}
	if restarted.Status().Paired || restarted.Paired() != nil {
		t.Fatal("revoked pairing restored from disk")
	}
}

func TestStatusConnectionReplacementAndDisconnect(t *testing.T) {
	svc := statusService(t)
	record, _ := statusPair(t, svc)
	requireStatusWakeup(t, svc)
	old, oldPeer := socketPair(t)
	svc.RegisterConnection(old)
	requireStatusWakeup(t, svc)
	connected := svc.Status()
	if !connected.Paired || !connected.Connected {
		t.Fatalf("missing connected status: %+v", connected)
	}
	next, nextPeer := socketPair(t)
	svc.RegisterConnection(next)
	requireStatusWakeup(t, svc)
	replaced := svc.Status()
	if replaced.Revision <= connected.Revision {
		t.Fatal("replacement did not advance revision")
	}
	oldPeer.Close()
	oldDone := make(chan struct{})
	go func() { defer close(oldDone); svc.RunReadLoop(old, record.BuddyID) }()
	select {
	case <-oldDone:
	case <-time.After(time.Second):
		t.Fatal("old reader did not terminate")
	}
	if got := svc.Status(); !reflect.DeepEqual(got, replaced) {
		t.Fatalf("stale reader changed replacement status: %+v", got)
	}
	requireNoStatusWakeup(t, svc)
	done := make(chan struct{})
	go func() { defer close(done); svc.RunReadLoop(next, record.BuddyID) }()
	nextPeer.Close()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("reader did not terminate")
	}
	requireStatusWakeup(t, svc)
	if got := svc.Status(); !got.Paired || got.Connected || got.BuddyID != record.BuddyID || got.Revision <= replaced.Revision {
		t.Fatalf("invalid disconnected status: %+v", got)
	}
}

func TestStatusPersistenceFailuresPreservePairing(t *testing.T) {
	svc := statusService(t)
	record, _ := statusPair(t, svc)
	requireStatusWakeup(t, svc)
	before := svc.Status()
	if err := os.Remove(BuddiesFilePath); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(BuddiesFilePath, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(BuddiesFilePath+"/blocker", []byte("prevent replacement"), 0o600); err != nil {
		t.Fatal(err)
	}
	code, _ := svc.IssuePairingCode()
	if _, err := svc.ConfirmPairing("Replacement", "other", "other", code); err == nil {
		t.Fatal("pairing succeeded despite persistence failure")
	}
	if err := svc.Unpair(); err == nil {
		t.Fatal("revoke succeeded despite persistence failure")
	}
	requireNoStatusWakeup(t, svc)
	if got := svc.Status(); !reflect.DeepEqual(got, before) {
		t.Fatalf("failed persistence changed status: %+v", got)
	}
	if got := svc.ValidateToken(record.Token); !reflect.DeepEqual(got, record) {
		t.Fatal("failed persistence invalidated the original pairing")
	}
}

func TestStatusNotificationsCoalesceWithoutBlocking(t *testing.T) {
	svc := statusService(t)
	initial := svc.Status()
	done := make(chan error, 1)
	const transitions = 20
	go func() {
		for i := 0; i < transitions; i++ {
			if err := svc.Unpair(); err != nil {
				done <- err
				return
			}
		}
		done <- nil
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("slow status consumer blocked pairing operations")
	}
	if got := svc.Status(); got.Revision != initial.Revision+transitions || got.InstanceID != initial.InstanceID {
		t.Fatalf("invalid revision after coalescing: %+v", got)
	}
	requireStatusWakeup(t, svc)
	requireNoStatusWakeup(t, svc)
}

func TestStatusAuthenticatedConnectionRejectsStaleTokens(t *testing.T) {
	svc := statusService(t)
	original, _ := statusPair(t, svc)
	requireStatusWakeup(t, svc)
	old, oldPeer := socketPair(t)
	if !svc.RegisterAuthenticatedConnection(old, original.Token) {
		t.Fatal("current token rejected")
	}
	requireStatusWakeup(t, svc)
	connected := svc.Status()
	if !connected.Paired || !connected.Connected {
		t.Fatalf("authenticated connection missing from status: %+v", connected)
	}

	replacement, _ := statusPair(t, svc)
	requireStatusWakeup(t, svc)
	replaced := svc.Status()
	if !replaced.Paired || replaced.Connected || replaced.BuddyID != replacement.BuddyID || replaced.Revision <= connected.Revision {
		t.Fatalf("replacement inherited old connection: %+v", replaced)
	}
	if err := oldPeer.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, _, err := oldPeer.ReadMessage(); err == nil {
		t.Fatal("replacement did not close old connection")
	} else if networkErr, ok := err.(interface{ Timeout() bool }); ok && networkErr.Timeout() {
		t.Fatal("old connection remained open until deadline")
	}

	stale, _ := socketPair(t)
	if svc.RegisterAuthenticatedConnection(stale, original.Token) {
		t.Fatal("replaced token registered a connection")
	}
	requireNoStatusWakeup(t, svc)
	if got := svc.Status(); !reflect.DeepEqual(got, replaced) || svc.Connected() {
		t.Fatalf("rejected token changed state: %+v", got)
	}

	current, _ := socketPair(t)
	if !svc.RegisterAuthenticatedConnection(current, replacement.Token) {
		t.Fatal("replacement token rejected")
	}
	requireStatusWakeup(t, svc)
	if got := svc.Status(); !got.Connected || got.Revision <= replaced.Revision {
		t.Fatalf("replacement connection missing from status: %+v", got)
	}
	if err := svc.Unpair(); err != nil {
		t.Fatal(err)
	}
	requireStatusWakeup(t, svc)
	revoked := svc.Status()
	if svc.RegisterAuthenticatedConnection(stale, replacement.Token) {
		t.Fatal("revoked token registered a connection")
	}
	requireNoStatusWakeup(t, svc)
	if got := svc.Status(); !reflect.DeepEqual(got, revoked) || svc.Connected() {
		t.Fatalf("revoked token changed state: %+v", got)
	}
}
