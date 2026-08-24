package bootstrap

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"strings"
	"sync"
	"time"
)

// OTA security modes reported to operators.
const (
	// SecurityModeVerified means a signing public key is pinned on this device,
	// so metadata signatures and artifact checksums are enforced.
	SecurityModeVerified = "verified"
	// SecurityModeLegacy means no key is pinned: the worker reads the unsigned
	// top-level component entries as a migration bridge.
	SecurityModeLegacy = "legacy"
)

// SecurityStatus is the operator-visible OTA trust posture of this device.
// Logs answered this before; the fleet needs it as a queryable surface so a
// device stuck in legacy mode is visible without shelling in.
type SecurityStatus struct {
	// Mode is SecurityModeVerified or SecurityModeLegacy.
	Mode string `json:"mode"`
	// MetadataFormat is the signed envelope format this worker accepts.
	MetadataFormat string `json:"metadata_format"`
	// KeyFingerprint identifies the pinned public key (first 16 hex characters
	// of its SHA-256) so operators can tell which key a device trusts without
	// exposing the key itself. Empty in legacy mode.
	KeyFingerprint string `json:"key_fingerprint,omitempty"`
	// ArtifactChecksums reports whether component SHA-256 digests are required.
	// They are only enforceable when the metadata carrying them is authentic.
	ArtifactChecksums bool `json:"artifact_checksums"`
	// LastMetadataFetch describes the most recent metadata fetch, or nil when
	// no fetch has completed since this worker started.
	LastMetadataFetch *MetadataFetchResult `json:"last_metadata_fetch,omitempty"`
}

// MetadataFetchResult records the outcome of one metadata fetch.
type MetadataFetchResult struct {
	At       time.Time `json:"at"`
	Verified bool      `json:"verified"`
	Error    string    `json:"error,omitempty"`
}

// securityTracker holds the last metadata fetch outcome for SecurityStatus.
type securityTracker struct {
	mu   sync.Mutex
	last *MetadataFetchResult
}

func (t *securityTracker) record(verified bool, err error) {
	result := &MetadataFetchResult{At: time.Now().UTC(), Verified: verified}
	if err != nil {
		result.Error = err.Error()
	}
	t.mu.Lock()
	t.last = result
	t.mu.Unlock()
}

func (t *securityTracker) snapshot() *MetadataFetchResult {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.last == nil {
		return nil
	}
	copied := *t.last
	return &copied
}

// securityStatus reports the OTA trust posture of this device.
func (b *Bootstrap) securityStatus() SecurityStatus {
	status := SecurityStatus{
		Mode:              SecurityModeLegacy,
		MetadataFormat:    otaMetadataFormat,
		LastMetadataFetch: b.security.snapshot(),
	}
	if key := strings.TrimSpace(b.cfg.SigningPublicKey); key != "" {
		status.Mode = SecurityModeVerified
		status.ArtifactChecksums = true
		status.KeyFingerprint = publicKeyFingerprint(key)
	}
	return status
}

// publicKeyFingerprint returns a short, stable identifier for a provisioned
// key. It hashes the raw key bytes when the value decodes, and the configured
// string otherwise, so a malformed key still yields a comparable fingerprint
// instead of an empty field.
func publicKeyFingerprint(encodedPublicKey string) string {
	encodedPublicKey = strings.TrimSpace(encodedPublicKey)
	raw, err := base64.StdEncoding.DecodeString(encodedPublicKey)
	if err != nil {
		raw = []byte(encodedPublicKey)
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])[:16]
}
