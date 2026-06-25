package buddy

import "log"

// LogActivitySink implements httpapi.ActivitySink by logging each Claude Code
// push. This is the seam where device feedback plugs in: a future
// HALActivitySink will implement the same interface and drive the LED / round
// display / voice via the bridge, with no change to the HTTP delivery layer.
type LogActivitySink struct{}

func (LogActivitySink) Notify(level, title, subtitle string, sound bool) {
	log.Printf("[activity] notify level=%q title=%q subtitle=%q sound=%v",
		level, title, subtitle, sound)
}

func (LogActivitySink) Usage(fiveHour, sevenDay int, reset5h, reset7d string, sound bool) {
	log.Printf("[activity] usage 5h=%d%% (reset %s) 7d=%d%% (reset %s) sound=%v",
		fiveHour, reset5h, sevenDay, reset7d, sound)
}
