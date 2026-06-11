package device

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

var (
	reFrontMatter     = regexp.MustCompile(`(?s)^---\s*\n(.*?)\n---\s*\n`)
	reGatewayBlock    = regexp.MustCompile(`(?m)^gateway:[ \t]*\n((?:[ \t]+.*\n?)+)`)
	reGatewayDefault  = regexp.MustCompile(`(?m)^[ \t]+default:[ \t]*(\S+)`)
	reGatewayProtocol = regexp.MustCompile(`(?m)^[ \t]+protocol:[ \t]*(\S+)`)
	reSoulRef         = regexp.MustCompile(`(?m)^soul_ref:[ \t]*(\S+)`)
)

// SoulRef returns the `soul_ref` declared in devices/<deviceType>/DEVICE.md, or
// "" if absent/unreadable. The value is either a path (read relative to the
// device dir) or an http(s) URL (downloaded) — see openclaw.deviceSoulCore.
// Dependency-free front-matter parse, mirroring GatewayDefault.
func SoulRef(deviceType string) string {
	b, err := os.ReadFile(filepath.Join(DevicesDir(), deviceType, "DEVICE.md"))
	if err != nil {
		return ""
	}
	fm := reFrontMatter.FindSubmatch(b)
	if fm == nil {
		return ""
	}
	m := reSoulRef.FindSubmatch(fm[1])
	if m == nil {
		return ""
	}
	return strings.TrimSpace(string(m[1]))
}

// DevicesDir resolves the per-device profile root (devices/<type>/...).
// DEVICES_DIR env wins; falls back to /opt/devices (mirrors HAL + onboarding.go).
func DevicesDir() string {
	if d := os.Getenv("DEVICES_DIR"); d != "" {
		return d
	}
	return "/opt/devices"
}

// gatewayField extracts one sub-field (matched by re) from the `gateway:` block
// of devices/<deviceType>/DEVICE.md, or "" if absent/unreadable. Dependency-free
// front-matter parse (no YAML lib), mirroring hal/board/device.py.
func gatewayField(deviceType string, re *regexp.Regexp) string {
	b, err := os.ReadFile(filepath.Join(DevicesDir(), deviceType, "DEVICE.md"))
	if err != nil {
		return ""
	}
	fm := reFrontMatter.FindSubmatch(b)
	if fm == nil {
		return ""
	}
	blk := reGatewayBlock.FindSubmatch(fm[1])
	if blk == nil {
		return ""
	}
	m := re.FindSubmatch(blk[1])
	if m == nil {
		return ""
	}
	return strings.TrimSpace(string(m[1]))
}

// GatewayDefault returns the `gateway.default` (agentic runtime) declared in
// devices/<deviceType>/DEVICE.md, or "" if absent.
func GatewayDefault(deviceType string) string {
	return gatewayField(deviceType, reGatewayDefault)
}

// GatewayProtocol returns the `gateway.protocol` (wire transport) declared in
// devices/<deviceType>/DEVICE.md, or "" if absent. The transport is actually a
// property of the runtime (openclaw→websocket, hermes→sse), so this is consumed
// only as a consistency guard — see agent.ProvideGateway.
func GatewayProtocol(deviceType string) string {
	return gatewayField(deviceType, reGatewayProtocol)
}
