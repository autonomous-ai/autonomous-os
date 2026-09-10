package i18n

// Dead-air fillers — short TTS cues spoken while OpenClaw is busy. Two
// pools per language (Opening for first filler of a turn, Continuation for
// re-arm after a tool finishes) plus per-tool overrides so the spoken
// filler stays conversational rather than narrating internal work.
//
// Looked up via:
//   - FillerOpening(lang)        — short acknowledgement at turn start
//   - FillerContinuation(lang)   — neutral "still working" between tools
//   - FillerForTool(lang, tool)  — tool-keyed override; nil when no entry,
//                                  caller falls back to FillerContinuation.

var fillerOpening = map[string][]string{
	LangEN: {
		"Mm", "Uh-huh", "Hmm",
	},
	LangVI: {
		"Ừm", "Ừ", "Ờ",
	},
	LangZhCN: {
		"嗯", "哦", "呃",
	},
	LangZhTW: {
		"嗯", "哦", "呃",
	},
}

var fillerContinuation = map[string][]string{
	LangEN: {
		"Mm", "Hmm", "Uh-huh",
	},
	LangVI: {
		"Ừm", "Ờ", "Ừ",
	},
	LangZhCN: {
		"嗯", "呃", "哦",
	},
	LangZhTW: {
		"嗯", "呃", "哦",
	},
}

// toolFillers indexes per-lang per-tool override pools. Tool name list
// keyed by exceptional physical states. Ordinary runtime tools fall back to
// fillerContinuation so the device does not narrate its internal work.
var toolFillers = map[string]map[string][]string{
	LangEN: {
		"look_searching":       {"Mm", "Hmm"},
		"look_still_searching": {"Mm", "Hmm"},
		"look_capturing":       {"Mm", "Hmm"},
		"look_found":           {"Found you"},
		"look_lost":            {"Can't see you"},
	},
	LangVI: {
		"look_searching":       {"Ừm", "Ờ"},
		"look_still_searching": {"Ừm", "Ờ"},
		"look_capturing":       {"Ừm", "Ờ"},
		"look_found":           {"Thấy rồi"},
		"look_lost":            {"Không thấy"},
	},
	LangZhCN: {
		"look_searching":       {"嗯", "呃"},
		"look_still_searching": {"嗯", "呃"},
		"look_capturing":       {"嗯", "呃"},
		"look_found":           {"找到了"},
		"look_lost":            {"看不见"},
	},
	LangZhTW: {
		"look_searching":       {"嗯", "呃"},
		"look_still_searching": {"嗯", "呃"},
		"look_capturing":       {"嗯", "呃"},
		"look_found":           {"找到了"},
		"look_lost":            {"看不見"},
	},
}

// FillerOpening returns the opening (first-of-turn) filler pool for lang.
// Falls back to English on unknown / empty lang.
func FillerOpening(lang string) []string {
	if p, ok := fillerOpening[lang]; ok && len(p) > 0 {
		return applyNameAll(p)
	}
	return applyNameAll(fillerOpening[fallbackLang])
}

// FillerContinuation returns the continuation (between-tools) filler pool
// for lang. Falls back to English on unknown / empty lang.
func FillerContinuation(lang string) []string {
	if p, ok := fillerContinuation[lang]; ok && len(p) > 0 {
		return applyNameAll(p)
	}
	return applyNameAll(fillerContinuation[fallbackLang])
}

// FillerForTool returns the tool-specific override pool for (lang, tool).
// Returns nil when no override exists — caller falls back to
// FillerContinuation. Unknown lang routes to the English pool.
func FillerForTool(lang, tool string) []string {
	if tool == "" {
		return nil
	}
	pools, ok := toolFillers[lang]
	if !ok {
		pools = toolFillers[fallbackLang]
	}
	return applyNameAll(pools[tool])
}
