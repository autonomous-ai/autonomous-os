package mqtthandler

import (
	"context"
	"fmt"
	"path/filepath"
	"sync"
)

// oauthConnectorWriter is the ConnectorWriter for Google OAuth connectors
// (gmail, google_calendar, google_drive). It clones the Google OAuth credential
// model rather than the remote-MCP model: persist + refresh an OAuth2 token, but
// with NO openclaw.json side-effect.
//
// It is intentionally the mcpConnectorWriter minus the mcp.servers.<name> entry.
// There is no hosted Google MCP URL to point at for these services, so unlike
// notion/figma/etc. there is nothing to write into openclaw.json — the
// credential is stored for downstream consumers and kept fresh by the connector
// refresh loop. Each connector owns its own <code>_access_tokens.json (the same
// per-connector file convention the MCP writer uses), isolated from the shared
// connectors.json and from the OAuth access_tokens.json.
type oauthConnectorWriter struct {
	mu sync.Mutex
	// name is the connector code AND the token filename stem (e.g. "gmail").
	// Also used for the stable error-message prefix ("<name>_writer: …").
	name string
	path string
}

// newOAuthConnectorWriter builds a writer for one Google OAuth connector.
// configsDir is typically `<OpenclawConfigDir>/workspace/configs`; the token
// lands in `<name>_access_tokens.json` there.
func newOAuthConnectorWriter(name, configsDir string) *oauthConnectorWriter {
	return &oauthConnectorWriter{
		name: name,
		path: filepath.Join(configsDir, name+"_access_tokens.json"),
	}
}

// Write persists credentials to <name>_access_tokens.json. No openclaw.json
// entry — that is the whole point of cloning the OAuth flow instead of the MCP
// flow. Error prefix mirrors mcpConnectorWriter ("<name>_writer: token file: …").
func (w *oauthConnectorWriter) Write(ctx context.Context, creds ConnectorCreds) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	file, err := loadConnectorsFile(w.path)
	if err != nil {
		return fmt.Errorf("%s_writer: token file: %w", w.name, err)
	}
	file.Connectors[creds.Connector] = connectorEntryFromCreds(creds)
	if err := writeConnectorsFile(w.path, file); err != nil {
		return fmt.Errorf("%s_writer: token file: %w", w.name, err)
	}
	return nil
}

// Remove deletes the token-file entry. Returns removed=true when it was present.
func (w *oauthConnectorWriter) Remove(ctx context.Context, connector string) (bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	file, err := loadConnectorsFile(w.path)
	if err != nil {
		return false, fmt.Errorf("%s_writer: token file: %w", w.name, err)
	}
	if _, ok := file.Connectors[connector]; !ok {
		return false, nil
	}
	delete(file.Connectors, connector)
	if err := writeConnectorsFile(w.path, file); err != nil {
		return false, fmt.Errorf("%s_writer: token file: %w", w.name, err)
	}
	return true, nil
}

// RefreshableEntries surfaces entries the refresh loop should rotate. Same
// universal rule as every writer: eligible only with BOTH a refresh_token AND
// refresh:true (the backend owns refresh eligibility via the connector.set
// `refresh` flag).
func (w *oauthConnectorWriter) RefreshableEntries() []ConnectorRefreshTarget {
	w.mu.Lock()
	defer w.mu.Unlock()
	file, err := loadConnectorsFile(w.path)
	if err != nil {
		return nil
	}
	out := make([]ConnectorRefreshTarget, 0, 1)
	for code, entry := range file.Connectors {
		if entry.RefreshToken == "" || !entry.Refresh {
			continue
		}
		out = append(out, ConnectorRefreshTarget{
			Connector:    code,
			RefreshToken: entry.RefreshToken,
			ExpiresAt:    entry.ExpiresAt,
		})
	}
	return out
}

// loadEntry returns the current on-disk entry for a connector. Satisfies the
// entryLoader interface so the refresh loop preserves fields the BE refresh
// response does not re-send (scopes, client_id, credentials) across a rotation.
func (w *oauthConnectorWriter) loadEntry(connector string) (ConnectorCreds, bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	file, err := loadConnectorsFile(w.path)
	if err != nil {
		return ConnectorCreds{}, false, err
	}
	entry, ok := file.Connectors[connector]
	if !ok {
		return ConnectorCreds{}, false, nil
	}
	return credsFromEntry(connector, entry), true, nil
}
