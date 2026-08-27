// Audio rules — volume, speaker mute, music stop, TTS interrupt.
package intent

import (
	"fmt"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/lib/hal"
)

// volumeStep returns the volume (%) a "louder"/"quieter" command should set,
// as a fraction of the device's real usable range.
//
// The range is the SAFETY.md `audio.max_volume` ceiling when one is declared,
// else the full scale. Without this, the steps are fractions of 100 on a device
// whose ceiling is far below it — on Lamp (ceiling 40) a fixed "quieter" of 30
// lands within a few percent of "louder", so the two commands stop being
// distinguishable. HAL clamps regardless; this only keeps the steps meaningful.
func volumeStep(fraction float64) int {
	top := 100
	if ceiling, ok := hal.MaxVolume(); ok {
		top = ceiling
	}
	return int(float64(top) * fraction)
}

var audioRules = []rule{
	// --- Volume ---
	{
		name:       "volume_up",
		capability: device.CapAudio,
		match:      anyOf("volume up", "louder"),
		exec: func(string) *Result {
			vol := volumeStep(1.0) // as loud as this device is allowed to go
			body := fmt.Sprintf(`{"volume":%d}`, vol)
			post("/audio/volume", body)
			return &Result{TTSText: "Volume up!", Actions: []string{"POST /audio/volume " + body}}
		},
	},
	{
		name:       "volume_down",
		capability: device.CapAudio,
		match:      anyOf("volume down", "quieter"),
		exec: func(string) *Result {
			vol := volumeStep(0.3) // same 30% of the usable range as before
			body := fmt.Sprintf(`{"volume":%d}`, vol)
			post("/audio/volume", body)
			return &Result{TTSText: "Volume down!", Actions: []string{"POST /audio/volume " + body}}
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
