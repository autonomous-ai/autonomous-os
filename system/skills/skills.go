// Package skills holds the platform skill catalog and the hardware capability
// each skill requires. This is OS-level metadata, independent of which agentic
// runtime (OpenClaw, Hermes, or any other) actually loads the skills into its
// workspace — so it lives here, not in a runtime package. Runtimes consume
// Catalog + Supported to decide which skills to provision for a given device.
package skills

import "go.autonomous.ai/os/system/device"

// Catalog and Capability are generated from the skills/ tree into
// catalog_gen.go — one folder per skill, capabilities declared in
// skills/<name>/skill.json. Add a skill by adding its folder, then run
// `make skills-catalog`; TestCatalogMatchesTree fails if you forget.
//
// The capability requirement lives in the sidecar rather than in SKILL.md
// front matter so the agentic runtime's skill header stays the standard
// name/description schema and never sees a non-standard key.

// Supported filters the catalog to the skills a device with deviceCaps can run:
// a skill is kept when it requires no capability (platform skill) or the device
// declares AT LEAST ONE of the skill's required capabilities (any-of). Fail-open:
// empty deviceCaps retains legacy skills. Optional environment sensing always
// requires an explicit environment capability, including on legacy devices.
func Supported(deviceCaps map[string]bool) []string {
	out := make([]string, 0, len(Catalog))
	for _, name := range Catalog {
		if name == "environment" && !deviceCaps[device.CapEnvironment] {
			continue
		}
		reqs := Capability[name]
		if len(deviceCaps) == 0 || len(reqs) == 0 || hasAny(deviceCaps, reqs) {
			out = append(out, name)
		}
	}
	return out
}

// hasAny reports whether deviceCaps declares at least one of reqs.
func hasAny(deviceCaps map[string]bool, reqs []string) bool {
	for _, c := range reqs {
		if deviceCaps[c] {
			return true
		}
	}
	return false
}
