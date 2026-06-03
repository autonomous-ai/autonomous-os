package openclaw

import (
	_ "embed"
	"fmt"
	"os"
	"path/filepath"
)

// figmaMCPServerJS is the first-party Figma REST MCP stdio server, embedded at
// build time and dropped onto the device by the figma-api connector writer.
// Token is supplied at runtime via the FIGMA_ACCESS_TOKEN env in the
// mcp.servers.figma-api entry — never baked into this script.
//
//go:embed assets/figma_mcp_server.mjs
var figmaMCPServerJS []byte

const figmaMCPServerFileMode = 0o644

// FigmaMCPServerPath returns the on-disk location of the Figma REST MCP wrapper
// for a given OpenClaw config dir: <configDir>/workspace/figma-mcp/server.mjs.
// Used as the `node` argument in the stdio mcp.servers.figma-api entry.
func FigmaMCPServerPath(configDir string) string {
	return filepath.Join(configDir, "workspace", "figma-mcp", "server.mjs")
}

// EnsureFigmaMCPServer writes the embedded Figma REST MCP wrapper to disk
// (overwriting any previous copy so updates ship with the binary) and returns
// its path. The gateway runs it via `node <path>`. chowns to the gateway
// runtime user when running as root, mirroring writeOpenclawConfig.
func EnsureFigmaMCPServer(configDir string) (string, error) {
	path := FigmaMCPServerPath(configDir)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return "", fmt.Errorf("mkdir figma-mcp dir: %w", err)
	}
	if err := os.WriteFile(path, figmaMCPServerJS, figmaMCPServerFileMode); err != nil {
		return "", fmt.Errorf("write figma mcp server: %w", err)
	}
	if err := chownRuntimeUserIfRoot(path, openclawRuntimeUser); err != nil {
		// Non-fatal: the gateway can still read it; log-and-continue parity with
		// writeOpenclawConfig's chown handling.
		return path, nil
	}
	return path, nil
}
