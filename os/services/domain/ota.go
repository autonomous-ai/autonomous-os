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
)

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
