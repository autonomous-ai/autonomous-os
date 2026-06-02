package mqtthandler

import (
	"context"
	"fmt"
	"path/filepath"
	"sync"

	"go-lamp.autonomous.ai/domain"
)

// mcpConnectorWriter is the single ConnectorWriter implementation for all
// remote-MCP connectors (Notion, Figma, Asana, …). The 90% shared logic —
// persist a per-connector token file + write the mcp.servers.<name> entry into
// openclaw.json (and restart the gateway) — lives here once. The 10% that
// differs between connectors is supplied via mcpConnectorConfig.
//
// The openclaw.json side-effect goes through domain.AgentGateway (lamp always
// runs OpenClaw, so the entry write always restarts the gateway — there is no
// hermes/ActiveAgent branch to honour).
type mcpConnectorWriter struct {
	mu      sync.Mutex
	cfg     mcpConnectorConfig
	path    string
	gateway domain.AgentGateway
}

// mcpConnectorConfig parametrizes the per-connector bits. Everything else is
// shared in mcpConnectorWriter's methods.
type mcpConnectorConfig struct {
	// name is the connector code AND the mcp.servers.<name> key (e.g. "notion").
	// Also used for the error-message prefix ("<name>_writer: …") and to derive
	// the token filename when tokenFile is empty.
	name string
	// mcpURL is the remote MCP endpoint written into mcp.servers.<name>.url.
	mcpURL string
	// header builds the Authorization header value from creds. Defaults to
	// "Bearer " + AccessToken (bearerAccessToken) when nil. Static-API-key
	// connectors (e.g. Ahrefs) supply bearerAPIKey instead.
	header func(c ConnectorCreds) string
	// tokenFile overrides the token filename. Empty → "<name>_access_tokens.json".
	tokenFile string
}

// bearerAccessToken is the default Authorization builder: "Bearer <access_token>".
// Used by OAuth connectors (Notion, Linear, Asana, Figma, GitHub) whose token
// lands in the access_token field.
func bearerAccessToken(c ConnectorCreds) string { return "Bearer " + c.AccessToken }

// bearerAPIKey builds "Bearer <api_key>" for connectors authenticated with a
// static API key rather than an OAuth access token (e.g. Ahrefs — the user
// pastes a self-generated MCP key, no OAuth, no refresh). The key arrives in
// the api_key field of connector.set, not access_token.
func bearerAPIKey(c ConnectorCreds) string { return "Bearer " + c.APIKey }

// newMCPConnectorWriter builds a writer from cfg. configsDir is typically
// `<OpenclawConfigDir>/workspace/configs`.
func newMCPConnectorWriter(cfg mcpConnectorConfig, configsDir string, gw domain.AgentGateway) *mcpConnectorWriter {
	if cfg.header == nil {
		cfg.header = bearerAccessToken
	}
	tokenFile := cfg.tokenFile
	if tokenFile == "" {
		tokenFile = cfg.name + "_access_tokens.json"
	}
	return &mcpConnectorWriter{
		cfg:     cfg,
		path:    filepath.Join(configsDir, tokenFile),
		gateway: gw,
	}
}

// Write persists credentials to <name>_access_tokens.json then writes the
// matching mcp.servers.<name> entry into openclaw.json. If the second step
// fails the credentials are still on disk — the refresh loop / a later
// connector.set will retry the openclaw side without re-fetching tokens.
//
// Error prefixes are stable ("<name>_writer: token file: …" / "<name>_writer:
// mcp entry: …") so the handler/refresh-loop build consistent failure strings.
func (w *mcpConnectorWriter) Write(ctx context.Context, creds ConnectorCreds) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	// 1. Persist token file (own schema, isolated from connectors.json).
	file, err := loadConnectorsFile(w.path)
	if err != nil {
		return fmt.Errorf("%s_writer: token file: %w", w.cfg.name, err)
	}
	file.Connectors[creds.Connector] = connectorEntryFromCreds(creds)
	if err := writeConnectorsFile(w.path, file); err != nil {
		return fmt.Errorf("%s_writer: token file: %w", w.cfg.name, err)
	}

	// 2. Push the Authorization header into openclaw.json + restart the gateway.
	entry := map[string]any{
		"type": "http",
		"url":  w.cfg.mcpURL,
		"headers": map[string]any{
			"Authorization": w.cfg.header(creds),
		},
	}
	if err := w.gateway.WriteMCPEntry(w.cfg.name, entry); err != nil {
		return fmt.Errorf("%s_writer: mcp entry: %w", w.cfg.name, err)
	}
	return nil
}

// Remove deletes the token file entry and the openclaw.json MCP entry. Returns
// removed=true when the token entry was present.
func (w *mcpConnectorWriter) Remove(ctx context.Context, connector string) (bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	file, err := loadConnectorsFile(w.path)
	if err != nil {
		return false, fmt.Errorf("%s_writer: token file: %w", w.cfg.name, err)
	}
	_, hadToken := file.Connectors[connector]
	if hadToken {
		delete(file.Connectors, connector)
		if err := writeConnectorsFile(w.path, file); err != nil {
			return false, fmt.Errorf("%s_writer: token file: %w", w.cfg.name, err)
		}
	}
	if _, err := w.gateway.RemoveMCPEntry(w.cfg.name); err != nil {
		return hadToken, fmt.Errorf("%s_writer: mcp entry: %w", w.cfg.name, err)
	}
	return hadToken, nil
}

// RefreshableEntries surfaces entries the refresh loop should rotate.
//
// Universal rule: refresh eligibility is owned by the backend via the
// connector.set `refresh` flag (persisted as entry.Refresh). An entry is
// refreshed only when it carries BOTH a refresh_token AND refresh:true.
// Absent/false flag → device never auto-refreshes that entry, regardless of
// connector. The BE is the single source of truth for rotation.
func (w *mcpConnectorWriter) RefreshableEntries() []ConnectorRefreshTarget {
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
// entryLoader interface used by the refresh loop for full-fidelity token merge.
func (w *mcpConnectorWriter) loadEntry(connector string) (ConnectorCreds, bool, error) {
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
