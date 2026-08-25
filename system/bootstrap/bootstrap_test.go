package bootstrap

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/system/bootstrap/config"
	"go.autonomous.ai/os/system/bootstrap/state"
	"go.autonomous.ai/os/system/domain"
)

func TestDecodeOTAMetadataRequiresValidSignatureAndChecksum(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	payload := []byte(`{"os-server":{"version":"1.2.3","url":"https://example.test/os.zip","sha256":"039058c6f2c0cb492c533b0a4d14ef77cc0f78abccced5287d84a1a2011cfb81"}}`)
	var envelope signedOTAMetadata
	envelope.Format = otaMetadataFormat
	envelope.Payload = base64.StdEncoding.EncodeToString(payload)
	envelope.Signature.Algorithm = "ed25519"
	envelope.Signature.Value = base64.StdEncoding.EncodeToString(ed25519.Sign(privateKey, payload))
	encoded, err := json.Marshal(envelope)
	if err != nil {
		t.Fatal(err)
	}

	metadata, err := decodeOTAMetadata(encoded, base64.StdEncoding.EncodeToString(publicKey))
	if err != nil {
		t.Fatalf("decode signed metadata: %v", err)
	}
	if metadata[domain.OTAKeyOSServer].Version != "1.2.3" {
		t.Fatalf("decoded wrong metadata: %+v", metadata)
	}
	var legacyCompatible map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &legacyCompatible); err != nil {
		t.Fatal(err)
	}
	legacyCompatible["signed"] = encoded
	hybrid, err := json.Marshal(legacyCompatible)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := decodeOTAMetadata(hybrid, base64.StdEncoding.EncodeToString(publicKey)); err != nil {
		t.Fatalf("hybrid metadata rejected: %v", err)
	}

	envelope.Payload = base64.StdEncoding.EncodeToString([]byte(`{"os-server":{"version":"9.9.9"}}`))
	tampered, err := json.Marshal(envelope)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := decodeOTAMetadata(tampered, base64.StdEncoding.EncodeToString(publicKey)); err == nil {
		t.Fatal("tampered metadata was accepted")
	}
}

func TestVerifyArtifactSHA256(t *testing.T) {
	data := []byte("abc")
	if err := verifyArtifactSHA256(data, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"); err != nil {
		t.Fatalf("valid checksum rejected: %v", err)
	}
	if err := verifyArtifactSHA256(data, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"); err == nil {
		t.Fatal("wrong checksum was accepted")
	}
}

func TestCompareVersions(t *testing.T) {
	cases := []struct {
		a, b string
		want int
	}{
		{"1.2.3", "1.2.3", 0},
		{"1.2.3", "1.2.4", -1},
		{"1.3.0", "1.2.9", 1},
		{"2.0.0", "1.9.9", 1},
		// numeric, not lexical: 27 > 9
		{"2026.5.27", "2026.5.9", 1},
		{"2026.5.9", "2026.5.27", -1},
		// pre-release/build suffix ignored (numeric core only)
		{"1.2.3-rc1", "1.2.3", 0},
		{"1.2.3+build5", "1.2.3", 0},
		// "v" prefix / surrounding text tolerated via semverRe extraction
		{"v1.4.0", "1.4.0", 0},
		// empty / unparseable sorts lowest
		{"", "0.0.1", -1},
		{"", "", 0},
		{"garbage", "1.0.0", -1},
	}
	for _, c := range cases {
		if got := compareVersions(c.a, c.b); got != c.want {
			t.Errorf("compareVersions(%q, %q) = %d, want %d", c.a, c.b, got, c.want)
		}
	}
}

// A component the device does not have must not read as "out of date". Before
// this gate, an absent artifact made detectVersion return "" — which sorts below
// every min_version floor — so the worker announced an update over the speaker,
// lit the OTA LED and tried to install it on every poll, forever.
func TestReconcileSkipsUninstalledComponent(t *testing.T) {
	devicesDir := t.TempDir() // no <type> subdir → the profile is not installed
	t.Setenv("DEVICES_DIR", devicesDir)
	t.Setenv("DEVICE_TYPE", "reachy-mini")

	b := &Bootstrap{state: &state.State{Components: map[string]string{}}}

	// Without the gate this reaches applyUpdate, which execs software-update and
	// returns an error — so a nil error is what proves the skip happened.
	updated, err := b.reconcile(context.Background(), domain.OTAKeyDevice,
		domain.OTAComponent{Version: "9.9.9"})
	if err != nil {
		t.Fatalf("reconcile errored on a component this device does not have: %v", err)
	}
	if updated {
		t.Fatal("reconcile reported an update for a component this device does not have")
	}
	if v, ok := b.state.Components[domain.OTAKeyDevice]; ok {
		t.Fatalf("a skipped component must not be written to state, got %q", v)
	}
}

func TestReconcileSkipsVersionBlockedByRollback(t *testing.T) {
	b := &Bootstrap{
		cfg:   &config.Config{RollbackVersions: map[string]string{domain.OTAKeyOSServer: "1.2.3"}},
		state: &state.State{Components: map[string]string{}},
	}
	updated, err := b.reconcile(context.Background(), domain.OTAKeyOSServer, domain.OTAComponent{
		Version:    "1.2.3",
		MinVersion: "1.2.3",
	})
	if err != nil {
		t.Fatalf("reconcile blocked version: %v", err)
	}
	if updated {
		t.Fatal("reconcile updated a version blocked by rollback")
	}
}

func TestComponentInstalled(t *testing.T) {
	devicesDir := t.TempDir()
	t.Setenv("DEVICES_DIR", devicesDir)
	t.Setenv("DEVICE_TYPE", "reachy-mini")
	b := &Bootstrap{}

	if b.componentInstalled(domain.OTAKeyDevice) {
		t.Error("device profile reported installed with no profile directory")
	}
	if err := os.Mkdir(filepath.Join(devicesDir, "reachy-mini"), 0o755); err != nil {
		t.Fatal(err)
	}
	if !b.componentInstalled(domain.OTAKeyDevice) {
		t.Error("device profile reported missing although its directory exists")
	}
	// The worker is the bootstrap component: always installed, so it can always
	// self-update.
	if !b.componentInstalled(domain.OTAKeyBootstrap) {
		t.Error("bootstrap must always count as installed")
	}
	// An unresolvable device type must not resolve to some other device's dir.
	t.Setenv("DEVICE_TYPE", "")
	if b.componentInstalled(domain.OTAKeyDevice) {
		t.Error("device profile reported installed with an unresolved device type")
	}
}

func TestCLISemver(t *testing.T) {
	cases := map[string]string{
		"OpenClaw 2026.3.8 (3caab92)":  "2026.3.8",
		"codex-cli 0.142.5":            "0.142.5",
		"2.1.218 (Claude Code)":        "2.1.218",
		"1.18.4":                       "1.18.4",
		"0.5.2\nextra line 9.9.9":      "0.5.2",
		"nightly-44-g1959045c-dirty":   "",
		"picoclaw nightly-44-g1959045": "",
	}
	for raw, want := range cases {
		if got := cliSemver(raw); got != want {
			t.Errorf("cliSemver(%q) = %q, want %q", raw, got, want)
		}
	}
}

func TestComponentInstalledAgentCLIsFollowRuntime(t *testing.T) {
	// The agent CLIs are gated on the configured runtime, not on the binary
	// being present — every lamp/intern-v2 image bakes all of them. With no
	// resolvable config (this test host), every CLI must report NOT installed so
	// the worker never pushes a runtime the device does not run.
	if _, err := os.Stat("/root/config/config.json"); err == nil {
		t.Skip("host has a real /root/config/config.json; runtime gate not isolatable")
	}
	b := &Bootstrap{}
	for _, key := range []string{domain.OTAKeyCodex, domain.OTAKeyClaudeCode, domain.OTAKeyOpenCode, domain.OTAKeyPicoClaw} {
		if b.componentInstalled(key) {
			t.Errorf("%s reported installed with an unresolvable agent_runtime", key)
		}
	}
}
