package bootstrap

import (
	"os"
	"path/filepath"
	"testing"
)

func TestAllowVendorApply(t *testing.T) {
	cases := []struct {
		name                     string
		signed, frozen, leftover bool
		ok                       bool
		reason                   string
	}{
		{"lab unsigned before handoff", false, false, false, true, ""},
		{"signed after handoff", true, false, true, true, ""},
		{"unsigned after handoff", false, false, true, false, "unsigned"},
		{"frozen signed", true, true, true, false, "frozen"},
		{"frozen unsigned", false, true, false, false, "frozen"},
	}
	for _, c := range cases {
		ok, reason := allowVendorApply(c.signed, c.frozen, c.leftover)
		if ok != c.ok || reason != c.reason {
			t.Fatalf("%s: got (%v, %q) want (%v, %q)", c.name, ok, reason, c.ok, c.reason)
		}
	}
}

func TestLoadVendorCustodyReadsABPRecord(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "custody.json")
	body := []byte(`{"owner":"Pat Demo","leftover_closed":true,"ota_frozen":true,"bindable_faces":["pat-face"]}`)
	if err := os.WriteFile(path, body, 0o600); err != nil {
		t.Fatal(err)
	}
	rec := loadVendorCustody(path)
	if !rec.LeftoverClosed || !rec.OtaFrozen {
		t.Fatalf("did not honor ABP custody flags: %+v", rec)
	}
}

func TestLoadVendorCustodyMissingIsOpen(t *testing.T) {
	rec := loadVendorCustody(filepath.Join(t.TempDir(), "missing.json"))
	if rec.LeftoverClosed || rec.OtaFrozen {
		t.Fatalf("missing custody file must stay legacy-open: %+v", rec)
	}
}
