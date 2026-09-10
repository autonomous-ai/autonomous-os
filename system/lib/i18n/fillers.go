package i18n

// Dead-air fillers — short TTS cues spoken while OpenClaw is busy. Two
// pools per language (Opening for first filler of a turn, Continuation for
// re-arm after a tool finishes) plus per-tool overrides so the spoken
// filler hints at what's happening without leaking machinery vocabulary.
//
// Looked up via:
//   - FillerOpening(lang)        — short acknowledgement at turn start
//   - FillerRealtime(lang)       — non-lexical cue while the realtime model waits
//   - FillerContinuation(lang)   — neutral "still working" between tools
//   - FillerForTool(lang, tool)  — tool-aware override; nil when no entry,
//                                  caller falls back to FillerContinuation.

var fillerOpening = map[string][]string{
	LangEN: {
		"Hmm, let me think", "Ok, got it", "Sure, one moment", "Right",
		"Got it", "Alright", "Ok", "Sure", "One sec",
	},
	LangVI: {
		"Hmm để xem", "Ờ rồi", "Vâng một chút", "Vâng", "Hiểu rồi",
		"Dạ", "Ờ", "Để xem", "Chờ chút",
	},
	LangZhCN: {
		"嗯，让我想想", "好的", "稍等一下", "好", "明白了",
		"嗯", "等一下", "稍等", "好的好的",
	},
	LangZhTW: {
		"嗯，讓我想想", "好的", "稍等一下", "好", "明白了",
		"嗯", "等一下", "稍等", "好的好的",
	},
}

// fillerRealtime is intentionally separate from fillerOpening. The user has
// already yielded the conversational floor when this plays, so it must sound
// like a quiet thinking sound rather than an acknowledgement or a promise.
var fillerRealtime = map[string][]string{
	LangEN:   {"Hmm...", "Mm..."},
	LangVI:   {"Ừm...", "Hừm..."},
	LangZhCN: {"嗯...", "呃..."},
	LangZhTW: {"嗯...", "呃..."},
}

var fillerContinuation = map[string][]string{
	LangEN: {
		"Hmm, let's see.", "Yeah, one sec.", "Let me try.",
		"Hang on a bit.", "Alright, let's look.",
	},
	LangVI: {
		"Ừm, để coi.", "Ờ, chờ tí.", "Hừm, để thử xem.",
		"À, để mình ngó.", "Ừ, để xem nào.",
	},
	LangZhCN: {
		"嗯，看看。", "等一下。", "让我试试。", "我看看。",
	},
	LangZhTW: {
		"嗯，看看。", "等一下。", "讓我試試。", "我看看。",
	},
}

// toolFillers indexes per-lang per-tool override pools. Tool name list
// sourced from OpenClaw runtime (web_search, web_fetch, read, memory_*,
// exec, image_generate, …). Only high-frequency / user-visible tools have
// entries — others fall back to fillerContinuation via FillerForTool.
var toolFillers = map[string]map[string][]string{
	LangEN: {
		// Look-aim states (hal/drivers/tracking/aim.py). Spoken only when the
		// aim actually has to search or takes long enough that the user is
		// already waiting — narrating every visual question gets old fast.
		"look_searching": {"Looking...", "Where are you?"},
		// Said ONCE, at the midpoint of a look-around, because the sweep is
		// about half a minute of the lamp swinging in silence and one phrase at
		// the start does not cover it. Repeating look_searching instead would
		// ask "where are you?" twice, which sounds stuck rather than patient.
		"look_still_searching": {"Still looking...", "Hmm..."},
		"look_found":           {"There you are.", "Found you."},
		// The resolution of an announced search that FAILED. look_searching
		// promises to look; without this the lamp turns away, says "Where are
		// you?", then goes quiet while the model describes whatever the camera
		// happened to be pointing at — the question answered about the wrong
		// thing, with nothing acknowledging that the search came up empty.
		"look_lost":      {"Can't see you.", "Lost you."},
		"look_capturing": {"Let's see.", "Hmm..."},
		"web_search":     {"Let's see.", "Hmm..."},
		"x_search":       {"Let's see.", "Checking."},
		"web_fetch":      {"Let's see.", "Reading."},
		"read":           {"Reading.", "Let's see."},
		"memory_search":  {"Let me think.", "Hmm..."},
		"memory_get":     {"Let me think.", "Hmm..."},
		"exec":           {"Trying it.", "One sec."},
		"process":        {"Trying it.", "One sec."},
		"image_generate": {"Let's try.", "Making it."},
		"video_generate": {"Let's try.", "Making it."},
		"music_generate": {"Let's try.", "Making it."},
		"update_plan":    {"Let's see.", "Hmm..."},
		"session_status": {"Let's see.", "Checking."},
		"apply_patch":    {"Fixing it.", "Let's try."},
		"pdf":            {"Reading.", "Let's see."},
		"canvas":         {"Let's try.", "Sketching."},
		"nodes":          {"Let's try.", "Hmm..."},
		"subagents":      {"Let's see.", "Hmm..."},
		"image":          {"Let's see.", "Looking."},
	},
	LangVI: {
		"look_searching":       {"Tìm thử...", "Bạn đâu rồi?"},
		"look_found":           {"À, đây rồi.", "Thấy rồi."},
		"look_lost":            {"Không thấy rồi.", "Mất dấu rồi."},
		"look_still_searching": {"Vẫn tìm đây...", "Hừm..."},
		"look_capturing":       {"Để xem.", "Hừm..."},
		"web_search":           {"Để coi.", "Hừm..."},
		"x_search":             {"Coi thử.", "Để coi."},
		"web_fetch":            {"Xem thử.", "Đọc chút."},
		"read":                 {"Đọc chút.", "Xem thử."},
		"memory_search":        {"Để nhớ.", "Hừm..."},
		"memory_get":           {"Nhớ xem.", "Hừm..."},
		"exec":                 {"Để thử.", "Làm tí."},
		"process":              {"Làm tí.", "Để thử."},
		"image_generate":       {"Vẽ tí.", "Để thử."},
		"video_generate":       {"Dựng tí.", "Để thử."},
		"music_generate":       {"Soạn tí.", "Để thử."},
		"update_plan":          {"Sắp lại tí.", "Để coi."},
		"session_status":       {"Xem lại tí.", "Để coi."},
		"apply_patch":          {"Sửa tí.", "Để thử."},
		"pdf":                  {"Đọc chút.", "Xem thử."},
		"canvas":               {"Vẽ tí.", "Để thử."},
		"nodes":                {"Để thử.", "Hừm..."},
		"subagents":            {"Nhờ chút.", "Để coi."},
		"image":                {"Xem chút.", "Để coi."},
	},
	LangZhCN: {
		"web_search":     {"我帮你找找", "查一下哦", "我去搜搜", "找一下啊"},
		"x_search":       {"去X看看", "瞅瞅X", "在X瞄一下"},
		"web_fetch":      {"我去看看", "翻开看看", "瞅一眼", "打开瞧瞧"},
		"read":           {"我看一下", "翻翻看", "瞄一眼", "我读读"},
		"memory_search":  {"我想想", "回忆一下", "翻翻记忆"},
		"memory_get":     {"我想想", "让我回忆下"},
		"exec":           {"我来弄", "马上做", "在做了", "正在弄"},
		"process":        {"我在弄", "后台跑着"},
		"image_generate": {"我来画", "画一张哦", "做一张看看", "画着呢"},
		"video_generate": {"我来弄", "在做呢"},
		"music_generate": {"在写曲子", "我来作曲"},
		"update_plan":    {"我重新理理", "再想想", "换个思路"},
		"session_status": {"我看看情况", "瞄一眼"},
		"apply_patch":    {"我来改", "调整一下"},
		"pdf":            {"我读一下", "扫一遍"},
		"canvas":         {"在画", "随手画一下"},
		"nodes":          {"我来", "马上"},
		"subagents":      {"找帮手", "叫人来帮"},
		"image":          {"我看看", "瞄一眼"},
	},
	LangZhTW: {
		"web_search":     {"我幫你找找", "查一下喔", "我去搜搜", "找一下啊"},
		"x_search":       {"去X看看", "瞄一下X", "在X瞧瞧"},
		"web_fetch":      {"我去看看", "翻開看看", "瞄一眼", "打開瞧瞧"},
		"read":           {"我看一下", "翻翻看", "瞄一眼", "我讀讀"},
		"memory_search":  {"我想想", "回憶一下", "翻翻記憶"},
		"memory_get":     {"我想想", "讓我回憶下"},
		"exec":           {"我來弄", "馬上做", "在做了", "正在弄"},
		"process":        {"我在弄", "背景跑著"},
		"image_generate": {"我來畫", "畫一張喔", "做一張看看", "畫著呢"},
		"video_generate": {"我來弄", "在做呢"},
		"music_generate": {"在寫曲子", "我來作曲"},
		"update_plan":    {"我重新理理", "再想想", "換個思路"},
		"session_status": {"我看看情況", "瞄一眼"},
		"apply_patch":    {"我來改", "調整一下"},
		"pdf":            {"我讀一下", "掃一遍"},
		"canvas":         {"在畫", "隨手畫一下"},
		"nodes":          {"我來", "馬上"},
		"subagents":      {"找幫手", "叫人來幫"},
		"image":          {"我看看", "瞄一眼"},
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

// FillerRealtime returns the dedicated pool for the realtime model wait.
// Falls back to English on unknown / empty lang.
func FillerRealtime(lang string) []string {
	if p, ok := fillerRealtime[lang]; ok && len(p) > 0 {
		return applyNameAll(p)
	}
	return applyNameAll(fillerRealtime[fallbackLang])
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
