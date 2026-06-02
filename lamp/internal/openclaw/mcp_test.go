package openclaw

import "testing"

func TestMCPConnectorURL(t *testing.T) {
	for _, name := range []string{"notion", "figma", "asana", "linear", "github", "ahrefs"} {
		url, ok := MCPConnectorURL(name)
		if !ok {
			t.Errorf("connector %q: expected known", name)
		}
		if url == "" {
			t.Errorf("connector %q: empty URL", name)
		}
		if !IsKnownMCPConnector(name) {
			t.Errorf("connector %q: IsKnownMCPConnector=false", name)
		}
	}

	for _, name := range []string{"dropbox", "", "default"} {
		if _, ok := MCPConnectorURL(name); ok {
			t.Errorf("connector %q: expected unknown", name)
		}
		if IsKnownMCPConnector(name) {
			t.Errorf("connector %q: IsKnownMCPConnector=true, want false", name)
		}
	}
}
