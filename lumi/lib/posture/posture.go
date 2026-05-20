// Package posture provides a per-user ergonomic-risk history logger.
//
// Three row types share one daily JSONL per user:
//
//   - `posture_alert` — auto-written by the sensing handler whenever a
//     motion.activity carries a [posture_summary:] block (i.e. a pose
//     tumbling window crossed POSE_BAD_RATIO). Captures latest_score,
//     latest_risk_level (mapped to high/medium/low/negligible), and the
//     per-side whole-body scores. This is the raw signal the habit skill
//     reads to compute posture_patterns (peak_hour, side_bias, typical_risk).
//   - `nudge_posture` / `praise_posture` — agent reactions fired through
//     /api/posture/log when the wellbeing skill decides to coach or
//     acknowledge a fix. Notes carry the spoken line.
//
// Mirrors lib/mood structure (newer pattern than lib/wellbeing). Daily
// JSONL files with 60-day retention.
//
// Usage:
//
//	posture.Init()                                                // once at startup
//	posture.LogAlert("gray", posture.AlertExtras{Score: 6, Risk: "medium", LeftScore: 5, RightScore: 6})
//	posture.LogNudge("gray", 5, "Cổ kìa, ngẩng lên thử.")
//	events := posture.Query("gray", "2026-05-13", 100)
package posture

import (
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go-lamp.autonomous.ai/lib/usercanon"
)

// Event is one posture history record persisted to JSONL.
//
// `Action` is the row type. Sub-fields are optional and only populated for
// rows that use them — JSON omitempty keeps each line compact.
type Event struct {
	TS     float64 `json:"ts"`
	Seq    int64   `json:"seq"`
	Hour   int     `json:"hour"`
	Action string  `json:"action"`

	// Alert-row fields (Action == ActionAlert).
	Score      int    `json:"score,omitempty"`
	Risk       string `json:"risk,omitempty"`        // medium | high (lelamp filters lower)
	LeftScore  int    `json:"left_score,omitempty"`
	RightScore int    `json:"right_score,omitempty"`

	// Nudge-row fields (Action == ActionNudge).
	NudgeLevel int    `json:"nudge_level,omitempty"` // 2..5

	// Free-text — for nudge/praise, this is the line Lumi spoke.
	Notes string `json:"notes,omitempty"`
}

// Action constants. Never invent new actions — the skill spec and timeline
// readers rely on this fixed vocabulary.
const (
	ActionAlert  = "posture_alert"  // sensing handler auto-write on bad-window motion.activity
	ActionNudge  = "nudge_posture"  // agent spoke / fired servo / chime
	ActionPraise = "praise_posture" // agent acknowledged a fix
)

const (
	postureSubdir = "posture"
	fileSuffix    = ".jsonl"
	// retentionDays is intentionally longer than music (7 days) and the
	// 30-day mood/wellbeing storage so the posture coach can do
	// weekly/monthly trend framing — "tuần này vs tuần trước", future
	// habit-integration patterns.
	// Daily file caps at ~16 KB → 60 days ≈ 960 KB / user. Negligible.
	retentionDays = 60
	DefaultUser   = "unknown"
)

type logger struct {
	mu   sync.Mutex
	seqN atomic.Int64
	file *os.File
	day  string
	user string
}

var global = &logger{}

// Init creates the users root and starts the retention cleaner.
// Call once at startup.
func Init() {
	_ = os.MkdirAll(usercanon.UsersDir, 0o755)
	go cleanOldLogs()
}

// AlertExtras carries the lelamp event payload Lumi persists when an event
// arrives. The skill is the caller — typically right when the event reaches
// it (before any nudge decision), so the timeline anchors each episode.
type AlertExtras struct {
	Score      int
	Risk       string
	LeftScore  int
	RightScore int
}

// LogAlert appends a `posture_alert` row capturing the lelamp event facts.
func LogAlert(user string, e AlertExtras) {
	user = usercanon.Resolve(user)
	now := time.Now()
	global.mu.Lock()
	defer global.mu.Unlock()
	evt := Event{
		TS:         float64(now.UnixNano()) / 1e9,
		Seq:        global.seqN.Add(1),
		Hour:       now.Hour(),
		Action:     ActionAlert,
		Score:      e.Score,
		Risk:       e.Risk,
		LeftScore:  e.LeftScore,
		RightScore: e.RightScore,
	}
	global.writeJSONL(now, user, evt)
}

// LogNudge appends a `nudge_posture` row after the agent has spoken / fired a
// servo / chime. level is 2..5 per the SKILL escalation ladder. notes is the
// line spoken (or empty for L2/L3 which have no voice).
func LogNudge(user string, level int, notes string) {
	user = usercanon.Resolve(user)
	now := time.Now()
	global.mu.Lock()
	defer global.mu.Unlock()
	evt := Event{
		TS:         float64(now.UnixNano()) / 1e9,
		Seq:        global.seqN.Add(1),
		Hour:       now.Hour(),
		Action:     ActionNudge,
		NudgeLevel: level,
		Notes:      notes,
	}
	global.writeJSONL(now, user, evt)
}

// LogPraise appends a `praise_posture` row.
func LogPraise(user, notes string) {
	logSimple(user, ActionPraise, notes)
}

func logSimple(user, action, notes string) {
	user = usercanon.Resolve(user)
	now := time.Now()
	global.mu.Lock()
	defer global.mu.Unlock()
	evt := Event{
		TS:     float64(now.UnixNano()) / 1e9,
		Seq:    global.seqN.Add(1),
		Hour:   now.Hour(),
		Action: action,
		Notes:  notes,
	}
	global.writeJSONL(now, user, evt)
}

// QueryLastDays reads posture events across the last `days` daily files
// (1 = today only, 7 = today + 6 prior). Events are returned oldest-first
// across the whole window. perDayCap limits rows per file (0 = no cap).
//
// Used by the skillcontext builder to compute a multi-day user profile
// (peak hour, side bias, weekly trend) without a fresh tool turn.
func QueryLastDays(user string, days, perDayCap int) []Event {
	user = usercanon.Resolve(user)
	if days <= 0 {
		days = 1
	}
	now := time.Now()
	out := make([]Event, 0, days*8)
	for i := days - 1; i >= 0; i-- {
		day := now.AddDate(0, 0, -i).Format("2006-01-02")
		out = append(out, Query(user, day, perDayCap)...)
	}
	return out
}

// Query reads posture events for the user/day. Up to last n rows. n<=0 → all.
func Query(user, day string, n int) []Event {
	user = usercanon.Resolve(user)
	path := filePath(user, day)
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) == 0 || (len(lines) == 1 && lines[0] == "") {
		return nil
	}
	if n > 0 && len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	out := make([]Event, 0, len(lines))
	for _, line := range lines {
		if line == "" {
			continue
		}
		var evt Event
		if err := json.Unmarshal([]byte(line), &evt); err == nil {
			out = append(out, evt)
		}
	}
	return out
}

// LastActionTS returns the Unix timestamp of the most recent row with the
// given action, scanning today and up to `lookbackDays-1` days back. Returns
// 0 when no match found.
func LastActionTS(user, action string, lookbackDays int) float64 {
	user = usercanon.Resolve(user)
	if lookbackDays <= 0 {
		lookbackDays = 1
	}
	now := time.Now()
	for i := 0; i < lookbackDays; i++ {
		day := now.AddDate(0, 0, -i).Format("2006-01-02")
		data, err := os.ReadFile(filePath(user, day))
		if err != nil {
			continue
		}
		lines := strings.Split(strings.TrimRight(string(data), "\n"), "\n")
		for j := len(lines) - 1; j >= 0; j-- {
			var evt Event
			if err := json.Unmarshal([]byte(lines[j]), &evt); err != nil {
				continue
			}
			if evt.Action == action {
				return evt.TS
			}
		}
	}
	return 0
}

// LastNudgeLevel returns the level of the most recent nudge_posture row today,
// or 0 if no nudge has been logged today.
func LastNudgeLevel(user string) int {
	events := Query(user, time.Now().Format("2006-01-02"), 0)
	for i := len(events) - 1; i >= 0; i-- {
		if events[i].Action == ActionNudge {
			return events[i].NudgeLevel
		}
	}
	return 0
}

func filePath(user, day string) string {
	return filepath.Join(usercanon.UsersDir, user, postureSubdir, day+fileSuffix)
}

// writeJSONL appends the event to the user's daily file. Must be called with
// l.mu held.
func (l *logger) writeJSONL(now time.Time, user string, evt Event) {
	day := now.Format("2006-01-02")
	if l.day != day || l.user != user || l.file == nil {
		if l.file != nil {
			_ = l.file.Close()
		}
		path := filePath(user, day)
		_ = os.MkdirAll(filepath.Dir(path), 0o755)
		f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			slog.Error("posture: failed to open log file", "path", path, "error", err)
			l.file = nil
			return
		}
		l.file = f
		l.day = day
		l.user = user
	}
	b, _ := json.Marshal(evt)
	_, _ = l.file.Write(append(b, '\n'))
}

// cleanOldLogs removes posture JSONL files older than retentionDays. Runs at
// startup and once a day after that.
func cleanOldLogs() {
	for {
		cutoff := time.Now().AddDate(0, 0, -retentionDays).Format("2006-01-02")
		entries, err := os.ReadDir(usercanon.UsersDir)
		if err == nil {
			for _, userDir := range entries {
				if !userDir.IsDir() {
					continue
				}
				dir := filepath.Join(usercanon.UsersDir, userDir.Name(), postureSubdir)
				files, err := os.ReadDir(dir)
				if err != nil {
					continue
				}
				for _, f := range files {
					name := f.Name()
					if !strings.HasSuffix(name, fileSuffix) {
						continue
					}
					day := strings.TrimSuffix(name, fileSuffix)
					if day < cutoff {
						_ = os.Remove(filepath.Join(dir, name))
					}
				}
			}
		}
		time.Sleep(24 * time.Hour)
	}
}
