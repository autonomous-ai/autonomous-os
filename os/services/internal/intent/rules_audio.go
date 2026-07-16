// Audio rules — volume, speaker mute, music stop, TTS interrupt.
package intent

import (
	"go.autonomous.ai/os/internal/device"
)

var audioRules = []rule{
	// --- Volume ---
	{
		name:       "volume_up",
		capability: device.CapAudio,
		match:      anyOf("volume up", "louder"),
		exec: func(string) *Result {
			post("/audio/volume", `{"volume":100}`)
			return &Result{TTSText: "Volume up!", Actions: []string{`POST /audio/volume {"volume":100}`}}
		},
	},
	{
		name:       "volume_down",
		capability: device.CapAudio,
		match:      anyOf("volume down", "quieter"),
		exec: func(string) *Result {
			post("/audio/volume", `{"volume":30}`)
			return &Result{TTSText: "Volume down!", Actions: []string{`POST /audio/volume {"volume":30}`}}
		},
	},
	// unmute before mute: belt-and-braces ordering on top of the
	// word-boundary match (containsPhrase) that already keeps "unmute
	// speaker" from hitting the mute keywords.
	{
		name:       "unmute_speaker",
		capability: device.CapMedia,
		match:      anyOf("unmute speaker", "unmute the speaker"),
		exec: func(string) *Result {
			post("/speaker/unmute", "")
			return &Result{TTSText: "Speaker on!", Actions: []string{`POST /speaker/unmute`}}
		},
	},
	{
		name:       "mute_speaker",
		capability: device.CapMedia,
		match:      anyOf("mute speaker", "mute the speaker"),
		exec: func(string) *Result {
			post("/speaker/mute", "")
			return &Result{TTSText: "", Actions: []string{`POST /speaker/mute`}}
		},
	},

	// --- Music control ---
	{
		name:       "music_stop",
		capability: device.CapMedia,
		match:      anyOf("stop music", "stop the music", "music off", "stop playing"),
		exec: func(string) *Result {
			post("/audio/stop", "")
			return &Result{TTSText: "Music stopped.", Actions: []string{"POST /audio/stop"}}
		},
	},

	// --- TTS stop (interrupt the device speaking) ---
	{
		name:       "stop_talking",
		capability: device.CapAudio,
		match:      anyOf("stop talking", "ok stop"),
		exec: func(string) *Result {
			post("/tts/stop", "")
			return &Result{TTSText: "", Actions: []string{"POST /tts/stop"}}
		},
	},
}
