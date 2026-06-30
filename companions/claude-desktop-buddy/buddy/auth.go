package buddy

import (
	"crypto/subtle"
	"encoding/json"
	"log"
	"os"
	"sync"
	"time"

	"golang.org/x/crypto/bcrypt"
)

// Authenticator validates the admin-password Bearer token the plugin presents
// on LAN-facing endpoints. The source of truth is the OS server's config.json
// (shared on /root/config): admin_password_hash is the bcrypt hash of the same
// password used to log into the web UI, so "the device password" means exactly
// one thing across web + buddy. llm_api_key is also accepted so machine callers
// (curl, scripts) keep working — this mirrors the OS server's adminAuthMiddleware.
//
// It fails closed: if neither credential is configured, Authorize always denies.
type Authenticator struct {
	path string

	mu       sync.Mutex
	modTime  time.Time
	hash     string // bcrypt(admin password)
	apiKey   string // llm_api_key machine token
	verified string // last plaintext that matched the current hash (bcrypt cache)
}

// NewAuthenticator reads credentials from the given OS server config.json path.
func NewAuthenticator(osConfigPath string) *Authenticator {
	a := &Authenticator{path: osConfigPath}
	a.refresh()
	if a.hash == "" && a.apiKey == "" {
		log.Printf("[auth] WARN: no admin_password_hash or llm_api_key in %s — all LAN endpoints will return 401 until set", osConfigPath)
	}
	return a
}

// refresh reloads credentials when config.json changes on disk, so a password
// rotation in the web UI takes effect without restarting the daemon. The bcrypt
// plaintext cache is cleared on reload because the hash may have changed.
func (a *Authenticator) refresh() {
	fi, err := os.Stat(a.path)
	if err != nil {
		return // keep last-known values if the file is briefly unreadable
	}
	if fi.ModTime().Equal(a.modTime) {
		return
	}
	data, err := os.ReadFile(a.path)
	if err != nil {
		return
	}
	var c struct {
		AdminPasswordHash string `json:"admin_password_hash"`
		LLMAPIKey         string `json:"llm_api_key"`
	}
	if err := json.Unmarshal(data, &c); err != nil {
		log.Printf("[auth] config parse error: %v (keeping previous credentials)", err)
		return
	}
	a.modTime = fi.ModTime()
	a.hash = c.AdminPasswordHash
	a.apiKey = c.LLMAPIKey
	a.verified = ""
}

// Authorize reports whether secret is the device admin password (or the machine
// API key). The correct-password hot path is a constant-time string compare
// against the cached plaintext, so bcrypt only runs on first use / after a
// rotation — not on every event the plugin pushes.
func (a *Authenticator) Authorize(secret string) bool {
	if secret == "" {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.refresh()

	if a.verified != "" && subtle.ConstantTimeCompare([]byte(secret), []byte(a.verified)) == 1 {
		return true
	}
	if a.apiKey != "" && subtle.ConstantTimeCompare([]byte(secret), []byte(a.apiKey)) == 1 {
		return true
	}
	if a.hash != "" && bcrypt.CompareHashAndPassword([]byte(a.hash), []byte(secret)) == nil {
		a.verified = secret
		return true
	}
	return false
}
