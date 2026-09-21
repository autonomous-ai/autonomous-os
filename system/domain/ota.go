package domain

// OTAComponent describes version and download URL for a single component.
//
// MinVersion is the "approved floor" the automatic OTA worker rolls devices up
// to. Bootstrap auto-updates a device only when its current version is BELOW
// MinVersion; manual `software-update <key>` over SSH ignores it and always
// installs Version. When MinVersion is empty it defaults to Version, so the
// auto worker simply tracks the latest. This lets a release bump Version (the
// build everyone CAN pull manually) without auto-pushing it to the fleet until
// MinVersion is promoted. Skills and hooks do not use MinVersion.
type OTAComponent struct {
	Version    string `json:"version"`
	MinVersion string `json:"min_version,omitempty"`
	URL        string `json:"url"`
	SHA256     string `json:"sha256,omitempty"`
	// Commit pins a git-installed component (hermes) to the exact upstream
	// commit that reports Version. Written by scripts/release/upload-hermes.sh;
	// `software-update hermes` checks that commit out through the upstream
	// installer's --commit flag. Empty on entries published before pinning
	// existed, which the updater treats as "unpinned: hermes update to HEAD".
	Commit string `json:"commit,omitempty"`
}

const (
	OTAKeyOSServer  = "os-server"
	OTAKeyBootstrap = "bootstrap"
	OTAKeyOpenClaw  = "openclaw"
	OTAKeyWeb       = "web"
	// OTAKeyHal's value is "hal" — the OTA metadata key, on-device deploy
	// dir (/opt/hal), and `software-update` arg.
	OTAKeyHal   = "hal"
	OTAKeyBuddy = "claude-desktop-buddy"
	// OTAKeyDevice is the on-device `software-update` arg for the device profile.
	// Unlike the others it is NOT a flat metadata key — the profile lives nested
	// at metadata.devices.<device_type> (one metadata.json serves all types).
	OTAKeyDevice = "device"

	// Agent-runtime CLIs. Each value is BOTH the metadata key and the
	// `software-update` arg, and each equals the runtime name in config.json
	// `agent_runtime` — bootstrap relies on that equality to update only the CLI
	// of the runtime a device actually runs (every lamp/intern-v2 image bakes ALL
	// of these binaries regardless of runtime, so binary presence proves nothing).
	//
	// OTAKeyHermes rides the same loop ONLY when the metadata entry carries a
	// Commit: Hermes is a git install and `hermes update` takes no target (it
	// moves to upstream HEAD), so an unpinned entry's min_version could never be
	// reached and would re-trigger every poll. With a commit, `software-update
	// hermes` checks that exact build out (see OTAComponent.Commit) and the
	// landed `hermes --version` matches the published semver like any other CLI.
	OTAKeyHermes     = "hermes"
	OTAKeyCodex      = "codex"
	OTAKeyClaudeCode = "claudecode"
	OTAKeyOpenCode   = "opencode"
	// OTAKeyPicoClaw's version is the GitHub release TAG (v0.3.1-fixvision), not
	// a semver: `picoclaw version` prints an unrelated build description, so the
	// installed tag is read from a stamp file instead (see detectVersion).
	OTAKeyPicoClaw = "picoclaw"
)

// PicoClawVersionStamp records the release tag `software-update picoclaw`
// installed. It exists because the PicoClaw binary cannot report its own tag.
const PicoClawVersionStamp = "/usr/local/lib/os-runtimes/picoclaw/installed-version"

// OTAMetadata is the JSON shape returned by the OTA metadata URL.
//
// Example:
//
//	{
//	  "lamp":    {"version":"1.2.3","url":"https://..."},
//	  "bootstrap": {"version":"2.3.4","url":"https://..."},
//	  "web":      {"version":"0.9.0","url":"https://..."}
//	}
type OTAMetadata map[string]OTAComponent
