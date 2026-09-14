package http

import "testing"

// A substring test against raw shell text turned a documentation read into a
// hardware event: `cat …/skills/emotion/SKILL.md` contains "/emotion", so the
// monitor recorded hw_emotion + led_set for a turn in which the lamp did
// nothing (#342 defect D, device-chat-13 seq 109). Anchoring on the HAL host
// is what separates a call from a mention.
func TestReadingASkillFileIsNotAHardwareEvent(t *testing.T) {
	for _, args := range []string{
		`{"command":"/bin/bash -lc 'cat /root/skills/emotion/SKILL.md | head -100'"}`,
		`{"command":"grep -rn /servo/aim /root/skills/"}`,
		`{"command":"cat docs/led-control.md"}`,
		`{"command":"echo 'the /emotion endpoint takes a name'"}`,
	} {
		if got := hwPathFromToolArgs(args); got != "" {
			t.Errorf("a non-call was treated as hardware: %q -> %q", args, got)
		}
	}
}

func TestARealHardwareCallIsResolvedToItsPath(t *testing.T) {
	for _, tc := range []struct{ args, want string }{
		{`{"command":"curl -sX POST http://127.0.0.1:5001/emotion -d '{\"emotion\":\"happy\"}'"}`, "/emotion"},
		{`{"command":"curl -sX POST http://127.0.0.1:5001/servo/aim -H 'Content-Type: application/json' -d '{\"direction\":\"right\"}'"}`, "/servo/aim"},
		{`{"command":"curl -s 'http://127.0.0.1:5001/camera/snapshot?save=true&width=768'"}`, "/camera/snapshot"},
		{`{"command":"curl -sX POST http://127.0.0.1:5000/api/vision/look -d '{}'"}`, "/api/vision/look"},
		{`/bin/bash -lc "curl -sX POST http://127.0.0.1:5001/led/off"`, "/led/off"},
	} {
		if got := hwPathFromToolArgs(tc.args); got != tc.want {
			t.Errorf("%s\n  got %q want %q", tc.args, got, tc.want)
		}
	}
}

// The servo branch knew only /servo/aim and /servo/play, so the one turn that
// actually swept (device-chat-9, a 42 s curl) showed an idle lamp in the
// monitor. Every endpoint that moves the body has to be in the set.
func TestEveryBodyMovingServoCallCountsAsAServoEvent(t *testing.T) {
	for _, path := range []string{
		"/servo/aim", "/servo/play", "/servo/search", "/servo/nudge", "/servo/demo",
	} {
		args := `{"command":"curl -sX POST http://127.0.0.1:5001` + path + ` -d '{}'"}`
		if !isServoMovementPath(hwPathFromToolArgs(args)) {
			t.Errorf("%s does not count as a servo event", path)
		}
	}
	for _, path := range []string{"/servo/position", "/servo/status", "/servo/bearing"} {
		args := `{"command":"curl -s http://127.0.0.1:5001` + path + `"}`
		if isServoMovementPath(hwPathFromToolArgs(args)) {
			t.Errorf("%s is a read, not a movement", path)
		}
	}
}

// The first HAL call in the text is the one that fires. A command that reads a
// skill and THEN calls the endpoint must resolve to the call, not to nothing.
func TestTheCallWinsOverAMentionInTheSameCommand(t *testing.T) {
	args := `{"command":"cat /root/skills/emotion/SKILL.md; curl -sX POST http://127.0.0.1:5001/emotion -d '{}'"}`
	if got := hwPathFromToolArgs(args); got != "/emotion" {
		t.Errorf("got %q want /emotion", got)
	}
}
