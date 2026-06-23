package hermes

import (
	"strings"
	"testing"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/runtimereg"
)

// The presync hook must be registered so os-server can materialize it on switch
// (which is what lets a plain OTA refresh it — install.sh only re-runs on a first
// install / failed verify).
func TestPresyncRegistered(t *testing.T) {
	script, ok := runtimereg.GetPresync(domain.AgentRuntimeHermes)
	if !ok || len(script) == 0 {
		t.Fatal("hermes presync not registered with runtimereg")
	}
}

// The presync hook OWNS config.yaml's model wiring: it must assert the static
// provider STRUCTURE (so it self-heals after a factory reset's `hermes setup
// --reset` wipes it) AND sync the dynamic per-device values. Guard the key pieces
// so the embedded script can't silently drift back to a partial/broken patch.
func TestPresyncOwnsConfigStructure(t *testing.T) {
	s := string(PresyncScript)
	for _, want := range []string{
		`.model.provider = "custom:autonomous"`,        // static provider
		`.custom_providers[0].api_mode = "anthropic_messages"`, // static routing mode
		`.custom_providers[0].name     = "autonomous"`,         // static provider name
		`.model.default = "Auto-AI"`,                           // fixed campaign-api model alias (NOT openclaw llm_model)
		`yq -i '.model = {}'`,                                  // coerce model:'' (post hermes setup --reset) to a map
		"AUTONOMOUS_API_KEY",                                   // dynamic key sync (.env)
	} {
		if !strings.Contains(s, want) {
			t.Errorf("presync.sh missing %q — config structure/sync incomplete", want)
		}
	}
}

// install.sh must NOT carry its own config.yaml patch or a presync heredoc anymore
// — both are owned by presync.sh / os-server materialization. A regression here
// would re-introduce the activation gap (install.sh-only fix never reaching an
// already-installed hermes).
func TestInstallDelegatesConfigToPresync(t *testing.T) {
	s := string(InstallScript)
	if strings.Contains(s, ".custom_providers = [") {
		t.Error("install.sh still patches .custom_providers — must delegate to presync.sh")
	}
	if !strings.Contains(s, "runtime-hermes-presync") {
		t.Error("install.sh must invoke the materialized presync hook")
	}
}
