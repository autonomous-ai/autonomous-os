// LED rules — color, on/off, dim. led_color must run before the generic
// led_on/led_off so "turn the light red" doesn't hit the plain-white rule.
package intent

import (
	"fmt"
	"strings"

	"go.autonomous.ai/os/system/device"
)

// colorKeywords maps color keywords to RGB values.
// Checked in order — first match wins.
var colorKeywords = []struct {
	keywords []string
	rgb      [3]int
	name     string
}{
	{[]string{"yellow"}, [3]int{255, 220, 0}, "Yellow"},
	{[]string{"red"}, [3]int{255, 0, 0}, "Red"},
	{[]string{"green"}, [3]int{0, 200, 100}, "Green"},
	{[]string{"blue"}, [3]int{0, 100, 255}, "Blue"},
	{[]string{"cyan"}, [3]int{0, 200, 150}, "Cyan"},
	{[]string{"purple", "violet"}, [3]int{100, 50, 200}, "Purple"},
	{[]string{"orange"}, [3]int{255, 100, 0}, "Orange"},
	{[]string{"pink"}, [3]int{255, 80, 150}, "Pink"},
	{[]string{"white"}, [3]int{255, 255, 255}, "White"},
	{[]string{"warm"}, [3]int{255, 180, 100}, "Warm"},
}

// extractColor returns the RGB and name for the first color keyword found in t.
func extractColor(t string) ([3]int, string, bool) {
	for _, c := range colorKeywords {
		for _, kw := range c.keywords {
			if strings.Contains(t, kw) {
				return c.rgb, c.name, true
			}
		}
	}
	return [3]int{}, "", false
}

// isLEDOnCommand returns true if t contains a "turn on light" trigger phrase.
func isLEDOnCommand(t string) bool {
	triggers := []string{"turn on the light", "light on", "set color", "change color", "set the light"}
	for _, kw := range triggers {
		if strings.Contains(t, kw) {
			return true
		}
	}
	return false
}

var ledRules = []rule{
	// --- LED color (must be before generic LED on/off) ---
	{
		name:       "led_color",
		capability: device.CapLight,
		match: func(t string) bool {
			if !isLEDOnCommand(t) {
				return false
			}
			_, _, ok := extractColor(t)
			return ok
		},
		exec: func(t string) *Result {
			rgb, name, _ := extractColor(t)
			executionFailed := post("/led/effect/stop", "") != nil
			body := fmt.Sprintf(`{"color":[%d,%d,%d]}`, rgb[0], rgb[1], rgb[2])
			executionFailed = post("/led/solid", body) != nil || executionFailed
			return &Result{ExecutionFailed: executionFailed, TTSText: name + " light on!", LEDChanged: true, Actions: []string{"POST /led/effect/stop", "POST /led/solid " + body}}
		},
	},

	// --- LED on/off ---
	{
		name:       "led_on",
		capability: device.CapLight,
		match:      anyOf("turn on the light", "light on"),
		exec: func(string) *Result {
			executionFailed := post("/led/solid", `{"color":[255,220,180]}`) != nil
			executionFailed = postEmotion(`{"emotion":"happy","intensity":0.6}`) != nil || executionFailed
			return &Result{ExecutionFailed: executionFailed, TTSText: "Light on!", LEDChanged: true, Actions: []string{`POST /led/solid {"color":[255,220,180]}`, `POST /emotion {"emotion":"happy","intensity":0.6}`}}
		},
	},
	{
		name:       "led_off",
		capability: device.CapLight,
		match:      anyOf("turn off the light", "light off"),
		exec: func(string) *Result {
			// No emotion after /led/off: any emotion (even idle) re-lights the
			// strip with its own color, undoing the off the user just asked for
			// (the off user-state then makes LED restore "keep emotion color",
			// so it never goes back to black). Turn off → stay off.
			executionFailed := post("/led/off", "") != nil
			return &Result{ExecutionFailed: executionFailed, TTSText: "Light off!", LEDOff: true, Actions: []string{"POST /led/off"}}
		},
	},

	// --- Dim / brightness ---
	{
		name:       "dim",
		capability: device.CapLight,
		match:      anyOf("dim the light", "dimmer", "dim light"),
		exec: func(string) *Result {
			executionFailed := post("/led/solid", `{"color":[80,60,40]}`) != nil
			return &Result{ExecutionFailed: executionFailed, TTSText: "Dimmed.", LEDChanged: true, Actions: []string{`POST /led/solid {"color":[80,60,40]}`}}
		},
	},
}
