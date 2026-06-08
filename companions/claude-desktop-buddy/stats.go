package main

import (
	"encoding/json"
	"log"
	"os"
	"path/filepath"
)

// statsPath holds approval/denial counters across buddy restarts.
// Lives under /var/lib so it survives package upgrades that wipe
// /opt/claude-desktop-buddy/, and stays separate from the runtime
// config in /root/config/buddy.json so a config reset doesn't reset
// the lifetime stats.
const statsPath = "/var/lib/claude-desktop-buddy/stats.json"

// PersistedStats is the on-disk shape — keep the JSON keys short
// because Claude Desktop's status ack uses the same `appr` / `deny`
// abbreviations.
type PersistedStats struct {
	Approved int `json:"appr"`
	Denied   int `json:"deny"`
}

// LoadStats reads the counters from disk. Missing file is treated as
// zeros — first run on a device has nothing to load. Any other read
// or parse error logs once and falls back to zeros rather than
// failing startup, since stats are advisory.
func LoadStats() PersistedStats {
	var s PersistedStats
	data, err := os.ReadFile(statsPath)
	if err != nil {
		if !os.IsNotExist(err) {
			log.Printf("[stats] read %s: %v (starting from zero)", statsPath, err)
		}
		return s
	}
	if err := json.Unmarshal(data, &s); err != nil {
		log.Printf("[stats] parse %s: %v (starting from zero)", statsPath, err)
		return PersistedStats{}
	}
	log.Printf("[stats] loaded approved=%d denied=%d from %s", s.Approved, s.Denied, statsPath)
	return s
}

// SaveStats persists the counters. Best-effort — errors are logged and
// swallowed because losing a single tick is harmless and we don't
// want approval handlers to block on disk I/O.
func SaveStats(s PersistedStats) {
	if err := os.MkdirAll(filepath.Dir(statsPath), 0o755); err != nil {
		log.Printf("[stats] mkdir %s: %v", filepath.Dir(statsPath), err)
		return
	}
	data, err := json.Marshal(s)
	if err != nil {
		log.Printf("[stats] marshal: %v", err)
		return
	}
	if err := os.WriteFile(statsPath, data, 0o644); err != nil {
		log.Printf("[stats] write %s: %v", statsPath, err)
	}
}
