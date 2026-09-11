// Scene rules — activation (focus/reading/relax/movie/night/energize) and
// scene off. scene_off must run before the activation rules: an off-phrasing
// containing a scene name ("deactivate focus mode") would otherwise match the
// activation rule and re-enter the scene the user asked to leave.
package intent

import (
	"fmt"
	"strings"

	"go.autonomous.ai/os/system/device"
)

// Words that mean "leave the scene" — English only; the intent layer always
// sees the translated voice-instruction text.
var sceneOffWords = []string{
	"turn off", "switch off", "disable", "deactivate", "exit", "stop",
	"cancel", "quit", "kill", "out of", "end ", " off",
}

var sceneNames = []string{"focus", "reading", "relax", "movie", "night", "energize"}

func hasSceneOffWord(t string) bool {
	for _, w := range sceneOffWords {
		if strings.Contains(t, w) {
			return true
		}
	}
	return false
}

func hasSceneName(t string) bool {
	for _, s := range sceneNames {
		if strings.Contains(t, s) {
			return true
		}
	}
	return false
}

// sceneOn matches a scene-activation phrase only when the text has no
// off-word — belt and braces on top of scene_off's ordering, so an off
// phrasing scene_off doesn't know still falls through to the agent instead
// of re-activating the scene the user asked to leave.
func sceneOn(keywords ...string) func(string) bool {
	m := anyOf(keywords...)
	return func(t string) bool {
		return m(t) && !hasSceneOffWord(t)
	}
}

func sceneExec(scene, reply string) func(string) *Result {
	return func(string) *Result {
		body := fmt.Sprintf(`{"scene":"%s"}`, scene)
		executionFailed := post("/scene", body) != nil
		return &Result{ExecutionFailed: executionFailed, TTSText: reply, LEDChanged: true, Actions: []string{"POST /scene " + body}}
	}
}

var sceneRules = []rule{
	// --- Scene off (must be before scene activation rules) ---
	{
		name:       "scene_off",
		capability: device.CapLight,
		match: func(t string) bool {
			// "Deactivate focus mode" used to slip past ("turn off"/"disable"
			// only) and fall through to scene_focus, which happily re-ACTIVATED
			// the scene the user asked to leave.
			return hasSceneOffWord(t) &&
				(strings.Contains(t, "mode") || strings.Contains(t, "scene") || hasSceneName(t))
		},
		exec: func(string) *Result {
			executionFailed := post("/scene/off", "") != nil
			return &Result{ExecutionFailed: executionFailed, TTSText: "Back to normal!", LEDOff: true, Actions: []string{"POST /scene/off"}}
		},
	},

	// --- Scenes ---
	{
		name:       "scene_reading",
		capability: device.CapLight,
		match:      sceneOn("reading mode", "reading light"),
		exec:       sceneExec("reading", "Reading mode!"),
	},
	{
		name:       "scene_focus",
		capability: device.CapLight,
		match:      sceneOn("focus mode", "focus light"),
		exec:       sceneExec("focus", "Focus mode!"),
	},
	{
		name:       "scene_relax",
		capability: device.CapLight,
		match:      sceneOn("relax mode", "relax light"),
		exec:       sceneExec("relax", "Relax mode!"),
	},
	{
		name:       "scene_movie",
		capability: device.CapLight,
		match:      sceneOn("movie mode", "movie light"),
		exec:       sceneExec("movie", "Movie mode!"),
	},
	{
		name:       "scene_night",
		capability: device.CapLight,
		match:      sceneOn("goodnight", "good night", "night mode"),
		exec: func(string) *Result {
			executionFailed := post("/scene", `{"scene":"night"}`) != nil
			executionFailed = postEmotion(`{"emotion":"sleepy","intensity":0.4}`) != nil || executionFailed
			return &Result{ExecutionFailed: executionFailed, TTSText: "Goodnight!", LEDChanged: true, Actions: []string{`POST /scene {"scene":"night"}`, `POST /emotion {"emotion":"sleepy","intensity":0.4}`}}
		},
	},
	{
		name:       "scene_energize",
		capability: device.CapLight,
		match:      sceneOn("brighter", "energize", "max brightness"),
		exec:       sceneExec("energize", "Max brightness!"),
	},
}
