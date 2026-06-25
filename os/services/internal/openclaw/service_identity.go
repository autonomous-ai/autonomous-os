package openclaw

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/lib/hal"
	"go.autonomous.ai/os/lib/i18n"
)

// --- Device identity (Ed25519) for gateway auth ---

const deviceKeyFile = "device-key.json"

// legacyDeviceKeyFile is the pre-debrand filename. resolveDeviceKey migrates it
// to deviceKeyFile so an already-paired device keeps its Ed25519 identity across
// the rename instead of generating a fresh key (which would force re-registration).
const legacyDeviceKeyFile = "lumi-device-key.json"

type deviceIdentity struct {
	PublicKey  ed25519.PublicKey
	PrivateKey ed25519.PrivateKey
	DeviceID   string // hex(SHA-256(publicKey))
}

// resolveDeviceKey reads the device key file, migrating a pre-debrand
// lumi-device-key.json to deviceKeyFile on first sight so an already-paired
// device keeps its identity. Returns the raw bytes, or an error if neither exists.
func (s *OpenclawService) resolveDeviceKey() ([]byte, error) {
	keyPath := filepath.Join(s.config.OpenclawConfigDir, deviceKeyFile)
	if data, err := os.ReadFile(keyPath); err == nil {
		return data, nil
	}
	legacyPath := filepath.Join(s.config.OpenclawConfigDir, legacyDeviceKeyFile)
	data, err := os.ReadFile(legacyPath)
	if err != nil {
		return nil, err
	}
	// Best-effort migrate to the new name; if the rewrite fails we still use the
	// bytes read this boot so identity is never lost.
	if werr := os.WriteFile(keyPath, data, 0600); werr == nil {
		_ = chownRuntimeUserIfRoot(keyPath, openclawRuntimeUser)
		_ = os.Remove(legacyPath)
		slog.Info("migrated legacy device key", "component", "openclaw", "from", legacyDeviceKeyFile, "to", deviceKeyFile)
	}
	return data, nil
}

// loadOrCreateDeviceIdentity loads the Ed25519 keypair from disk, or generates
// a new one and persists it for future connections.
func (s *OpenclawService) loadOrCreateDeviceIdentity() (*deviceIdentity, error) {
	keyPath := filepath.Join(s.config.OpenclawConfigDir, deviceKeyFile)
	if data, err := s.resolveDeviceKey(); err == nil {
		var stored struct {
			PrivateKey string `json:"privateKey"` // hex-encoded 64-byte Ed25519 seed+pub
		}
		if err := json.Unmarshal(data, &stored); err == nil {
			privBytes, err := hex.DecodeString(stored.PrivateKey)
			if err == nil && len(privBytes) == ed25519.PrivateKeySize {
				priv := ed25519.PrivateKey(privBytes)
				pub := priv.Public().(ed25519.PublicKey)
				id := deriveDeviceID(pub)
				slog.Info("loaded device identity", "component", "openclaw", "deviceId", id)
				return &deviceIdentity{PublicKey: pub, PrivateKey: priv, DeviceID: id}, nil
			}
		}
	}

	// Generate new keypair
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("generate ed25519 key: %w", err)
	}
	id := deriveDeviceID(pub)

	stored := map[string]string{"privateKey": hex.EncodeToString(priv)}
	data, _ := json.MarshalIndent(stored, "", "  ")
	if err := os.WriteFile(keyPath, data, 0600); err != nil {
		return nil, fmt.Errorf("write device key: %w", err)
	}
	_ = chownRuntimeUserIfRoot(keyPath, openclawRuntimeUser)
	slog.Info("generated new device identity", "component", "openclaw", "deviceId", id)
	return &deviceIdentity{PublicKey: pub, PrivateKey: priv, DeviceID: id}, nil
}

// deriveDeviceID returns hex(SHA-256(rawPublicKey)).
func deriveDeviceID(pub ed25519.PublicKey) string {
	h := sha256.Sum256(pub)
	return hex.EncodeToString(h[:])
}

// signConnectPayload builds and signs the v2 payload for device auth.
// Format: v2|deviceId|clientId|clientMode|role|scopes|signedAtMs|token|nonce
func (di *deviceIdentity) signConnectPayload(token, nonce string, signedAt int64) string {
	payload := fmt.Sprintf("v2|%s|%s|%s|%s|%s|%d|%s|%s",
		di.DeviceID,
		"node-host", // clientId
		"node",      // clientMode
		"operator",  // role
		"operator.read,operator.write,operator.admin", // scopes
		signedAt,
		token,
		nonce,
	)
	sig := ed25519.Sign(di.PrivateKey, []byte(payload))
	return base64.StdEncoding.EncodeToString(sig)
}

// WatchIdentity polls IDENTITY.md in the OpenClaw workspace and pushes updated wake words
// to HAL whenever the agent's name changes (e.g. user says "call yourself Noah").
func (s *OpenclawService) WatchIdentity(ctx context.Context) {
	identityPath := filepath.Join(s.config.OpenclawConfigDir, "workspace", "IDENTITY.md")
	var lastName string
	for {
		if !sleepCtx(ctx, 5*time.Second) {
			return
		}
		data, err := os.ReadFile(identityPath)
		if err != nil {
			continue
		}
		name := parseIdentityName(string(data))
		if name == "" || name == lastName {
			continue
		}
		lastName = name
		words := buildWakeWords(name)
		slog.Info("agent renamed, updating wake words", "component", "openclaw", "name", name, "words", words)
		hal.SetVoiceConfig(words)
		i18n.SetDeviceName(name) // {name}/{Name} + chitchat strip follow the agent name too
	}
}

// UpdateIdentityName rewrites the `**Name:**` line in workspace/IDENTITY.md so
// downstream consumers (parseIdentityName / WatchIdentity → wake words) see the
// new agent name. Preserves any bullet prefix and everything else in the file;
// appends a fresh `- **Name:** <name>` line when none exists yet. Atomic write
// via tmp+rename so a mid-write crash cannot truncate the file.
func (s *OpenclawService) UpdateIdentityName(name string) error {
	name = strings.TrimSpace(name)
	if name == "" {
		return fmt.Errorf("identity name is required")
	}

	identityPath := filepath.Join(s.config.OpenclawConfigDir, "workspace", "IDENTITY.md")
	dir := filepath.Dir(identityPath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}

	existing, err := os.ReadFile(identityPath)
	if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("read %s: %w", identityPath, err)
	}

	updated := rewriteIdentityName(string(existing), name)

	tmp, err := os.CreateTemp(dir, ".IDENTITY.*.tmp")
	if err != nil {
		return fmt.Errorf("create tmp: %w", err)
	}
	tmpPath := tmp.Name()
	if _, err := tmp.WriteString(updated); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("write tmp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("close tmp: %w", err)
	}
	if err := os.Rename(tmpPath, identityPath); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("rename: %w", err)
	}
	_ = chownRuntimeUserIfRoot(identityPath, openclawRuntimeUser)
	slog.Info("identity name updated", "component", "openclaw", "name", name, "path", identityPath)
	return nil
}

// rewriteIdentityName returns content with the first `**name:**` line's value
// replaced by name. The line's leading prefix (e.g. "- ") is preserved; any
// trailing description after the value is dropped. When no name line is found,
// "- **Name:** <name>" is appended (with a leading newline if needed).
func rewriteIdentityName(content, name string) string {
	lines := strings.Split(content, "\n")
	for i, line := range lines {
		idx := strings.Index(strings.ToLower(line), "**name:**")
		if idx < 0 {
			continue
		}
		lines[i] = line[:idx] + "**Name:** " + name
		// Drop a following italic-parens placeholder hint like
		// `  _(pick something you like)_` left over from the OpenClaw onboarding
		// template — once the user picks a name, the hint is stale.
		if i+1 < len(lines) && isItalicPlaceholder(lines[i+1]) {
			lines = append(lines[:i+1], lines[i+2:]...)
		}
		return strings.Join(lines, "\n")
	}
	// No existing name line — append one. Add a trailing newline only when the
	// file doesn't already end with one so we don't accumulate blank lines.
	prefix := content
	if prefix != "" && !strings.HasSuffix(prefix, "\n") {
		prefix += "\n"
	}
	return prefix + "- **Name:** " + name + "\n"
}

// isItalicPlaceholder returns true when line is a markdown italic note wrapped
// in `_(...)_` or `*(...)*` (with optional leading whitespace). Used to detect
// and remove OpenClaw's onboarding-template hint lines after rename.
func isItalicPlaceholder(line string) bool {
	t := strings.TrimSpace(line)
	if len(t) < 4 {
		return false
	}
	return (strings.HasPrefix(t, "_(") && strings.HasSuffix(t, ")_")) ||
		(strings.HasPrefix(t, "*(") && strings.HasSuffix(t, ")*"))
}

// parseIdentityName extracts the agent name from IDENTITY.md content.
// Looks for a line matching: - **Name:** <value>
func parseIdentityName(content string) string {
	for _, line := range strings.Split(content, "\n") {
		line = strings.TrimSpace(line)
		// Match: - **Name:** Lamp  or  **Name:** Lamp
		lower := strings.ToLower(line)
		idx := strings.Index(lower, "**name:**")
		if idx < 0 {
			continue
		}
		name := strings.TrimSpace(line[idx+len("**name:**"):])
		// Strip trailing markdown (e.g. " — some description")
		if i := strings.IndexAny(name, "—-|"); i > 0 {
			name = strings.TrimSpace(name[:i])
		}
		if name != "" {
			return name
		}
	}
	return ""
}

// buildWakeWords generates wake word variants from an agent name.
func buildWakeWords(name string) []string {
	n := strings.ToLower(name)
	return []string{
		"hey " + n,
		n,
		"này " + n,
		"ê " + n,
		n + " ơi",
	}
}
