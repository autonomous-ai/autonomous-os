package domain

// OTAComponent describes version and download URL for a single component.
type OTAComponent struct {
	Version string `json:"version"`
	URL     string `json:"url"`
}

const (
	OTAKeyLamp      = "lamp"
	OTAKeyBootstrap = "bootstrap"
	OTAKeyOpenClaw  = "openclaw"
	OTAKeyWeb       = "web"
	OTAKeyLeLamp    = "lelamp"
	OTAKeyBuddy     = "claude-desktop-buddy"
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
