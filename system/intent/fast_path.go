package intent

import "strings"

// matchCanonical executes only a complete, unqualified command locally. Legacy
// substring matching remains available to Match, but contextual user requests
// must reach semantic classification before any hardware side effect.
func matchCanonical(text string) *Result {
	fields := voiceFields(normalize(text))
	if len(fields) != 1 || fields[0] != normalize(text) {
		return nil
	}
	text = strings.Join(strings.Fields(strings.TrimRight(strings.TrimSpace(fields[0]), ".!?")), " ")
	if chitchatEnabled() {
		if result := matchChitchat(text); result != nil {
			return result
		}
	}
	text = strings.TrimPrefix(text, "please ")
	text = strings.TrimSuffix(text, " please")
	if canonicalCommand(text) {
		return MatchCommands(text)
	}
	return nil
}

func canonicalCommand(text string) bool {
	switch text {
	case "turn on the light", "light on", "turn off the light", "light off",
		"dim the light", "dimmer", "dim light", "volume up", "louder", "volume down", "quieter",
		"mute speaker", "mute the speaker", "unmute speaker", "unmute the speaker",
		"stop music", "stop the music", "music off", "stop playing", "stop talking", "ok stop",
		"stop tracking", "stop following", "stop watching", "stop track",
		"what time", "what time is it", "whats the time", "what's the time",
		"goodnight", "good night", "brighter", "energize", "max brightness":
		return true
	}
	for _, scene := range sceneNames {
		for _, noun := range []string{"mode", "light"} {
			if text == scene+" "+noun {
				return true
			}
		}
	}
	for _, verb := range []string{"turn off", "switch off", "disable", "deactivate", "exit", "stop", "cancel", "quit", "end"} {
		for _, noun := range []string{"scene", "mode"} {
			if text == verb+" "+noun || text == verb+" the "+noun {
				return true
			}
			for _, scene := range sceneNames {
				if text == verb+" "+scene+" "+noun || text == verb+" the "+scene+" "+noun {
					return true
				}
			}
		}
	}
	for _, color := range colorKeywords {
		for _, name := range color.keywords {
			for _, prefix := range []string{"set the light ", "turn on the light ", "light on ", "set color ", "change color "} {
				if text == prefix+name {
					return true
				}
			}
		}
	}
	for _, target := range trackTargets {
		for _, name := range target.keywords {
			for _, verb := range []string{"track ", "follow "} {
				if text == verb+name || text == verb+"the "+name {
					return true
				}
			}
		}
	}
	return false
}
