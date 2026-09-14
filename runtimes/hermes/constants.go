package hermes

// BaseURL, APIKey — mutable at runtime so the "remote" agent runtime can point
// the same client at a Hermes server on another machine (typically the user's
// Mac) via ApplyExternalEndpoint. See runtimes/hermes/remote.go. Conversation
// and Model stay stable — the "remote" runtime is still Hermes on the wire, it
// just runs somewhere else.
var (
	BaseURL      = "http://127.0.0.1:8642"
	APIKey       = "hermes-local-api-key"
	Conversation = "device-main"
	Model        = "hermes-agent"
)
