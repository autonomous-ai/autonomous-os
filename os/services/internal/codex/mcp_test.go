package codex

import (
	"testing"

	"github.com/pelletier/go-toml/v2"
)

// TestToCodexMCPEntryHTTP: the canonical OpenClaw-shaped hosted-MCP entry
// ({type:"http", url, headers}) maps to Codex's {url, http_headers} — type is
// dropped (transport inferred from url) and headers renamed.
func TestToCodexMCPEntryHTTP(t *testing.T) {
	entry := map[string]any{
		"type":    "http",
		"url":     "https://mcp.notion.com/mcp",
		"headers": map[string]any{"Authorization": "Bearer tok"},
	}
	got := toCodexMCPEntry(entry)
	if _, ok := got["type"]; ok {
		t.Fatalf("type key must be dropped, got %v", got)
	}
	if got["url"] != "https://mcp.notion.com/mcp" {
		t.Fatalf("url not preserved: %v", got)
	}
	if _, ok := got["headers"]; ok {
		t.Fatalf("headers must be renamed to http_headers, got %v", got)
	}
	hh, ok := got["http_headers"].(map[string]any)
	if !ok || hh["Authorization"] != "Bearer tok" {
		t.Fatalf("http_headers not mapped: %v", got)
	}
}

// TestToCodexMCPEntryStdio: stdio entries pass through unchanged.
func TestToCodexMCPEntryStdio(t *testing.T) {
	entry := map[string]any{
		"command": "npx",
		"args":    []any{"-y", "some-mcp"},
		"env":     map[string]any{"KEY": "v"},
	}
	got := toCodexMCPEntry(entry)
	for _, k := range []string{"command", "args", "env"} {
		if _, ok := got[k]; !ok {
			t.Fatalf("stdio key %q must pass through, got %v", k, got)
		}
	}
}

// TestCodexMCPEntryTOMLRoundTrip: the mapped entry survives a config.toml
// marshal/unmarshal cycle under [mcp_servers.<name>] — the same path
// WriteMCPEntry drives.
func TestCodexMCPEntryTOMLRoundTrip(t *testing.T) {
	cfg := map[string]any{
		"model":          "Auto-AI",
		"model_provider": "autonomous",
	}
	servers := ensureCodexMap(cfg, "mcp_servers")
	servers["notion"] = toCodexMCPEntry(map[string]any{
		"type":    "http",
		"url":     "https://mcp.notion.com/mcp",
		"headers": map[string]any{"Authorization": "Bearer tok"},
	})

	out, err := toml.Marshal(cfg)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	back := map[string]any{}
	if err := toml.Unmarshal(out, &back); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	srv, _ := back["mcp_servers"].(map[string]any)
	notion, _ := srv["notion"].(map[string]any)
	if notion == nil || notion["url"] != "https://mcp.notion.com/mcp" {
		t.Fatalf("round trip lost entry: %s", out)
	}
	if back["model"] != "Auto-AI" {
		t.Fatalf("round trip lost head keys: %s", out)
	}
}
