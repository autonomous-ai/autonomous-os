// Package osreset holds the one OS-level primitive the factory-reset path shares
// across packages that must not import each other: the device wipe (server/system)
// and each agent backend's ResetAgent (internal/<backend>). It lives in lib/ so a
// backend can use it without importing server/system (which would invert the
// dependency direction). Backend-specific helpers (service stop/verify) live with
// the backend that uses them, not here.
package osreset

import (
	"log"
	"os"
)

// WipePath removes path (recursively) and logs the outcome under prefix. Missing
// paths are silently ignored; a failure is logged non-fatally and skipped.
func WipePath(prefix, p string) {
	if _, err := os.Stat(p); os.IsNotExist(err) {
		return
	}
	if err := os.RemoveAll(p); err != nil {
		if os.IsNotExist(err) {
			return
		}
		log.Printf("%s wipe %s: %v (non-fatal)", prefix, p, err)
		return
	}
	log.Printf("%s wiped %s", prefix, p)
}
