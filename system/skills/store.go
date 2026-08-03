package skills

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Client for the Autonomous Agent Skills catalog (the public read API of
// `bff-web-service`, documented in agent-skills-public-api.md).
//
// It lives here rather than in one delivery package because two of them need it:
// the web UI proxy (system/server/agent/delivery/http/handler_skills.go) and the
// MQTT downlink (system/server/device/delivery/mqtt/skills_deploy_handler.go).
// Both must hit the same host with the same required header, so keeping one copy
// means neither can drift.

const (
	// StoreBaseURLDefault is the catalog host. Overridable via the
	// SKILL_STORE_BASE_URL env var so a device can be pointed at a local or
	// staging BFF without a rebuild.
	StoreBaseURLDefault = "https://apiv2.autonomous.ai"

	// storeLocation fills the `location` header the catalog's LocationHandler
	// middleware requires on every /api/v1 route. en-US is the documented
	// default/fallback.
	storeLocation = "en-US"

	// StoreDownloadTimeout bounds an archive download (bigger body than a
	// listing, so a longer budget).
	StoreDownloadTimeout = 30 * time.Second

	// StoreMaxBytes caps any single response read off the catalog.
	StoreMaxBytes = 16 << 20
)

// StoreBaseURL returns the configured catalog host without a trailing slash.
func StoreBaseURL() string {
	if env := strings.TrimSpace(os.Getenv("SKILL_STORE_BASE_URL")); env != "" {
		return strings.TrimRight(env, "/")
	}
	return StoreBaseURLDefault
}

// StoreGet performs a GET against the catalog with the required location header
// and returns the raw body. A non-200 response is an error; note that the catalog
// also reports BUSINESS failures as HTTP 200 with a non-1 `status` in its JSON
// envelope, so JSON callers must inspect that too — this only speaks HTTP.
func StoreGet(path string, query url.Values, timeout time.Duration, maxBytes int64) ([]byte, error) {
	u := StoreBaseURL() + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	req, err := http.NewRequest(http.MethodGet, u, nil)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("location", storeLocation)

	client := &http.Client{Timeout: timeout}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("request %s: %w", path, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("skill store returned %s", resp.Status)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxBytes))
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}
	return body, nil
}

// ValidateStoreSkillID rejects an id that could escape the upstream URL path.
// The catalog owns the id format (currently a Mongo ObjectID hex string), so this
// only blocks separators rather than pinning a shape.
func ValidateStoreSkillID(id string) error {
	id = strings.TrimSpace(id)
	if id == "" {
		return fmt.Errorf("skill id is required")
	}
	if strings.ContainsAny(id, "/\\?#") {
		return fmt.Errorf("invalid skill id %q", id)
	}
	return nil
}

// DownloadStoreArchive fetches the catalog's `.skill` archive for id and writes
// it inside destDir, returning the file path. The `/download` endpoint returns a
// raw zip, NOT the JSON envelope — so a JSON body here means the catalog failed,
// which surfaces as an invalid-archive error at extract time.
//
// Callers own destDir and must clean it up (os.MkdirTemp + defer os.RemoveAll).
func DownloadStoreArchive(id, destDir string) (string, error) {
	if err := ValidateStoreSkillID(id); err != nil {
		return "", err
	}

	archive, err := StoreGet("/api/v1/agent-skills/"+url.PathEscape(strings.TrimSpace(id))+"/download",
		nil, StoreDownloadTimeout, StoreMaxBytes)
	if err != nil {
		return "", fmt.Errorf("download skill %s: %w", id, err)
	}

	zipPath := filepath.Join(destDir, "skill.zip")
	if err := os.WriteFile(zipPath, archive, 0600); err != nil {
		return "", fmt.Errorf("write temp archive: %w", err)
	}
	return zipPath, nil
}
