package mqtthandler

import (
	"context"
	"path/filepath"
	"testing"
)

func TestOAuthConnectorWriter_WriteCreatesPerConnectorFile(t *testing.T) {
	dir := t.TempDir()
	w := newOAuthConnectorWriter("gmail", dir)

	if got, want := w.path, filepath.Join(dir, "gmail_access_tokens.json"); got != want {
		t.Fatalf("path = %q, want %q", got, want)
	}

	creds := ConnectorCreds{
		Connector:    "gmail",
		AuthType:     "oauth2",
		AccessToken:  "at-1",
		RefreshToken: "rt-1",
		TokenType:    "Bearer",
		ExpiresAt:    1234567890,
		Scopes:       []string{"https://www.googleapis.com/auth/gmail.modify"},
		UserEmail:    "u@example.com",
		ClientID:     "cid",
		Refresh:      true,
		ObtainedAt:   100,
	}
	if err := w.Write(context.Background(), creds); err != nil {
		t.Fatalf("Write: %v", err)
	}

	// File written under the per-connector path, and the entry round-trips.
	got, ok, err := w.loadEntry("gmail")
	if err != nil || !ok {
		t.Fatalf("loadEntry: ok=%v err=%v", ok, err)
	}
	if got.AccessToken != "at-1" || got.RefreshToken != "rt-1" || got.AuthType != "oauth2" {
		t.Fatalf("entry mismatch: %+v", got)
	}
	if len(got.Scopes) != 1 || got.Scopes[0] != "https://www.googleapis.com/auth/gmail.modify" {
		t.Fatalf("scopes not preserved: %+v", got.Scopes)
	}
}

func TestOAuthConnectorWriter_Remove(t *testing.T) {
	dir := t.TempDir()
	w := newOAuthConnectorWriter("google_drive", dir)
	ctx := context.Background()

	// Remove on absent entry is a no-op, no error.
	removed, err := w.Remove(ctx, "google_drive")
	if err != nil || removed {
		t.Fatalf("Remove(absent) = removed=%v err=%v, want false,nil", removed, err)
	}

	if err := w.Write(ctx, ConnectorCreds{Connector: "google_drive", AccessToken: "at"}); err != nil {
		t.Fatalf("Write: %v", err)
	}
	removed, err = w.Remove(ctx, "google_drive")
	if err != nil || !removed {
		t.Fatalf("Remove(present) = removed=%v err=%v, want true,nil", removed, err)
	}
	if _, ok, _ := w.loadEntry("google_drive"); ok {
		t.Fatalf("entry still present after Remove")
	}
}

func TestOAuthConnectorWriter_RefreshableEntriesGating(t *testing.T) {
	dir := t.TempDir()
	w := newOAuthConnectorWriter("google_calendar", dir)
	ctx := context.Background()

	// refresh:true but no refresh_token → not eligible.
	if err := w.Write(ctx, ConnectorCreds{Connector: "google_calendar", AccessToken: "at", Refresh: true}); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if got := w.RefreshableEntries(); len(got) != 0 {
		t.Fatalf("no refresh_token should be ineligible, got %+v", got)
	}

	// refresh_token present but refresh:false → not eligible.
	if err := w.Write(ctx, ConnectorCreds{Connector: "google_calendar", AccessToken: "at", RefreshToken: "rt", Refresh: false}); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if got := w.RefreshableEntries(); len(got) != 0 {
		t.Fatalf("refresh:false should be ineligible, got %+v", got)
	}

	// Both present → eligible.
	if err := w.Write(ctx, ConnectorCreds{Connector: "google_calendar", AccessToken: "at", RefreshToken: "rt", Refresh: true, ExpiresAt: 999}); err != nil {
		t.Fatalf("Write: %v", err)
	}
	got := w.RefreshableEntries()
	if len(got) != 1 || got[0].Connector != "google_calendar" || got[0].RefreshToken != "rt" || got[0].ExpiresAt != 999 {
		t.Fatalf("expected one eligible entry, got %+v", got)
	}
}

// oauthConnectorWriter must satisfy entryLoader so the refresh loop preserves
// fields the backend refresh response doesn't re-send.
func TestOAuthConnectorWriter_ImplementsEntryLoader(t *testing.T) {
	var _ entryLoader = newOAuthConnectorWriter("gmail", t.TempDir())
	var _ ConnectorWriter = newOAuthConnectorWriter("gmail", t.TempDir())
}
