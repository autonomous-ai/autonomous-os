package i18n

// Dead-air fillers — short TTS cues spoken while OpenClaw is busy. Two
// pools per language (Opening for first filler of a turn, Continuation for
// re-arm after a tool finishes) plus per-tool overrides so the spoken
// filler hints at what's happening without leaking machinery vocabulary.
//
// Looked up via:
//   - FillerOpening(lang)        — short acknowledgement at turn start
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

var fillerContinuation = map[string][]string{
	LangEN: {
		"Still on it", "Still thinking", "Let me check", "Hmm, processing",
		"Hang on", "Bear with me", "Still here", "One moment",
		"Working on it", "Just a sec", "Hmm, working", "Still digging",
	},
	LangVI: {
		"Vẫn đang nghĩ", "Để mình xem", "Đang xử lý nhé", "Đợi chút nhé",
		"Hmm, để xem", "Vẫn đây mà", "Mình đang làm tiếp", "Còn đang nghĩ",
		"Đang làm đây", "Chờ chút nha", "Để xem tí nữa", "Còn xử lý nhé",
	},
	LangZhCN: {
		"还在想", "让我看看", "我在处理", "稍等一下", "嗯，再想想",
		"我还在", "再等等", "还在弄", "我在搜", "再稍候", "继续找", "搜索中",
	},
	LangZhTW: {
		"還在想", "讓我看看", "我在處理", "稍等一下", "嗯，再想想",
		"我還在", "再等等", "還在弄", "我在搜", "再稍候", "繼續找", "搜尋中",
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
		"look_searching": {"Where are you?", "Let me find you", "Hold on, looking for you", "One sec, finding you"},
		// Said ONCE, at the midpoint of a look-around, because the sweep is
		// about half a minute of the lamp swinging in silence and one phrase at
		// the start does not cover it. Repeating look_searching instead would
		// ask "where are you?" twice, which sounds stuck rather than patient.
		"look_still_searching": {"Still looking", "One moment", "Nearly there", "Bear with me"},
		"look_found":           {"There you are", "Got you", "Found you"},
		// The resolution of an announced search that FAILED. look_searching
		// promises to look; without this the lamp turns away, says "Where are
		// you?", then goes quiet while the model describes whatever the camera
		// happened to be pointing at — the question answered about the wrong
		// thing, with nothing acknowledging that the search came up empty.
		"look_lost":      {"I can't find you", "I've lost you", "I can't see you from here"},
		"look_capturing": {"Let me see", "Having a look", "Taking a look"},
		"web_search":     {"Let me look that up", "Quick search", "Checking around", "Hunting that down"},
		"x_search":       {"Peeking at X", "Quick look on X", "Checking X"},
		"web_fetch":      {"Taking a peek", "Pulling that up", "Let me see", "Loading it up"},
		"read":           {"Reading through", "Let me see", "Skimming it", "Having a look"},
		"memory_search":  {"Digging through my notes", "Let me remember", "Checking what I know"},
		"memory_get":     {"Pulling that up", "Let me recall"},
		"exec":           {"On it", "Working on it", "Putting it together", "Crunching it"},
		"process":        {"On it", "Working in the background"},
		"image_generate": {"Painting it", "Making something", "Creating that", "Sketching it"},
		"video_generate": {"Putting it together", "Rolling the camera"},
		"music_generate": {"Composing", "Making the track"},
		"update_plan":    {"Rethinking", "Reshuffling things", "Taking another look"},
		"session_status": {"Taking stock", "Catching up"},
		"apply_patch":    {"Tweaking it", "Making the change"},
		"pdf":            {"Looking through it", "Skimming the doc"},
		"canvas":         {"Sketching", "Doodling it"},
		"nodes":          {"On it", "Reaching for that"},
		"subagents":      {"Calling for help", "Getting backup"},
		"image":          {"Taking a look", "Peeking at it"},
	},
	LangVI: {
		"look_searching":       {"Bạn đang ở đâu?", "Để tôi tìm bạn", "Chờ chút, tôi đang tìm bạn"},
		"look_found":           {"Bạn đây rồi", "Thấy bạn rồi"},
		"look_lost":            {"Tôi không tìm thấy bạn", "Tôi không thấy bạn đâu", "Từ đây tôi không thấy bạn"},
		"look_still_searching": {"Vẫn đang tìm", "Chờ chút nữa", "Sắp thấy rồi", "Đợi tôi tí"},
		"look_capturing":       {"Để tôi xem nào", "Tôi nhìn thử"},
		"web_search":           {"Để {Name} tìm chút", "Để xem có gì hay", "Lùng chút nha", "Tra cho bạn nha"},
		"x_search":             {"Ngó X tí", "Xem trên X chút", "Lùng X coi"},
		"web_fetch":            {"Để mình xem chút", "Mở ra xem nha", "Để {Name} ngó qua", "Coi thử nha"},
		"read":                 {"Để {Name} đọc qua", "Xem chút nha", "Lướt qua chút", "Để mình ngó"},
		"memory_search":        {"Để {Name} nhớ lại", "Lục trí nhớ chút", "Đợi {Name} nhớ ra"},
		"memory_get":           {"Để {Name} nhớ chút", "Đợi mình nhớ ra"},
		"exec":                 {"{Name} làm liền", "Đang làm cho bạn", "Đợi tí nha", "Mình lo nha"},
		"process":              {"Mình lo phần đó", "Đang làm phía sau"},
		"image_generate":       {"Để {Name} vẽ chút", "Đang vẽ nha", "Sáng tác chút", "Đợi {Name} tạo nha"},
		"video_generate":       {"Đang dựng cho bạn", "Để {Name} làm chút"},
		"music_generate":       {"Đang sáng tác nha", "Để {Name} soạn nhạc"},
		"update_plan":          {"Để {Name} sắp xếp lại", "Tính lại chút", "Nghĩ lại chút"},
		"session_status":       {"Để {Name} nhìn lại", "Coi tình hình chút"},
		"apply_patch":          {"Đang chỉnh chút", "Sửa giúp bạn"},
		"pdf":                  {"Để {Name} đọc qua", "Lướt qua chút"},
		"canvas":               {"Đang vẽ nha", "Phác chút coi"},
		"nodes":                {"{Name} làm liền", "Để mình lo nha"},
		"subagents":            {"Để {Name} nhờ phụ chút", "Gọi phụ tá nha"},
		"image":                {"Để {Name} nhìn nha", "Ngắm tí coi"},
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
