package bootstrap

import (
	"encoding/json"
	"os"
	"strings"
)

const defaultCustodyFile = "/var/lib/lamp/custody.json"

// vendorCustody is the ABP custody record fields this worker honors.
// Other fields are ignored. After handoff leftover_closed is true; the owner
// may freeze updates with ota_frozen.
type vendorCustody struct {
	LeftoverClosed bool `json:"leftover_closed"`
	OtaFrozen      bool `json:"ota_frozen"`
}

func allowVendorApply(signed, frozen, leftoverClosed bool) (bool, string) {
	if frozen {
		return false, "frozen"
	}
	if leftoverClosed && !signed {
		return false, "unsigned"
	}
	return true, ""
}

func loadVendorCustody(path string) vendorCustody {
	if strings.TrimSpace(path) == "" {
		path = defaultCustodyFile
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return vendorCustody{}
	}
	var rec vendorCustody
	if err := json.Unmarshal(data, &rec); err != nil {
		return vendorCustody{}
	}
	return rec
}

func (b *Bootstrap) custodyFile() string {
	if b != nil && b.cfg != nil && strings.TrimSpace(b.cfg.CustodyFile) != "" {
		return b.cfg.CustodyFile
	}
	return defaultCustodyFile
}
