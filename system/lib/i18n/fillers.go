package i18n

import "strings"

// Dead-air fillers — short TTS cues spoken while the agent is busy. Two
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
// normalised across runtimes by FillerToolKey. Unknown tools fall back to
// fillerContinuation via FillerForTool.
var toolFillers = map[string]map[string][]string{
	LangEN: {
		"search_files":   {"Looking it up.", "Let me check."},
		"memory_store":   {"Making a note.", "One sec."},
		"audio_generate": {"Preparing the audio.", "One sec."},
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
		"search_files":         {"Tìm chút.", "Để mình tra."},
		"memory_store":         {"Ghi lại tí.", "Chờ chút."},
		"audio_generate":       {"Chuẩn bị tiếng nhé.", "Chờ chút."},
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
		"search_files":   {"找一下。", "查查看。"},
		"memory_store":   {"记一下。", "等一下。"},
		"audio_generate": {"准备音频。", "等一下。"},
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
		"search_files":   {"找一下。", "查查看。"},
		"memory_store":   {"記一下。", "等一下。"},
		"audio_generate": {"準備音訊。", "等一下。"},
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

// FillerToolKey normalises raw runtime tool names into the small vocabulary
// used by toolFillers. Names from OpenClaw, Hermes, OpenCode, Codex and Harness do not
// share a wire-level enum, so unknown tools deliberately pass through and
// fall back to FillerContinuation.
func FillerToolKey(tool string) string {
	key := strings.ToLower(strings.TrimSpace(tool))
	key = strings.ReplaceAll(key, "-", "_")
	key = strings.ReplaceAll(key, ".", "_")

	// Match documented Hermes names before the legacy suffix heuristics:
	// session_search and spotify_search are not web searches. Only recognise
	// explicit names, including MCP-wrapped ones; unknown names stay intact.
	name := key
	if strings.HasPrefix(name, "mcp__") {
		if i := strings.LastIndex(name, "__"); i > len("mcp__") {
			name = name[i+2:]
		}
	}
	// Source: https://hermes-agent.nousresearch.com/docs/reference/tools-reference
	// Reviewed 2026-09-11. Keep the registry coverage test and EN/VI docs in sync.
	switch name {
	case "terminal", "execute_code":
		return "exec"
	case "process", "web_search", "x_search", "image_generate", "video_generate", "search_files":
		return name
	case "read_file", "skill_view", "skills_list", "read_terminal", "read_preview",
		"browser_console", "browser_snapshot", "browser_get_images", "feishu_doc_read",
		"feishu_drive_list_comments", "feishu_drive_list_comment_replies":
		return "read"
	case "write_file", "patch", "skill_manage":
		return "apply_patch"
	case "web_extract", "browser_navigate", "browser_back":
		return "web_fetch"
	case "browser_click", "browser_press", "browser_scroll", "browser_type",
		"browser_cdp", "browser_dialog", "computer_use", "drive_preview",
		"annotate_preview", "open_preview", "close_preview", "close_terminal", "focus_pane":
		return "nodes"
	case "browser_vision", "vision_analyze", "video_analyze":
		return "image"
	case "memory", "honcho_conclude":
		return "memory_store"
	case "session_search", "honcho_search":
		return "memory_search"
	case "honcho_profile", "honcho_context", "honcho_reasoning":
		return "memory_get"
	case "delegate_task":
		return "subagents"
	case "todo", "cronjob", "kanban_complete", "kanban_request_review",
		"kanban_request_changes", "kanban_comment", "kanban_create", "kanban_link",
		"kanban_unblock", "kanban_attach", "kanban_attach_url", "project_create", "project_switch":
		return "update_plan"
	case "xai_video_edit", "xai_video_extend":
		return "video_generate"
	case "text_to_speech":
		return "audio_generate"
	case "ha_call_service":
		return "nodes"
	case "spotify_search", "yb_search_sticker":
		return "search_files" // Neutral lookup phrases also fit a non-web catalog.
	case "clarify", "kanban_block", "kanban_show", "kanban_list", "kanban_heartbeat",
		"kanban_attachments", "project_list", "ha_get_state", "ha_list_entities", "ha_list_services",
		"read_window_below", "react_to_message", "tour", "tip", "discord", "discord_admin",
		"feishu_drive_add_comment", "feishu_drive_reply_comment", "spotify_playback",
		"spotify_devices", "spotify_queue", "spotify_playlists", "spotify_albums", "spotify_library",
		"yb_query_group_info", "yb_query_group_members", "yb_send_dm", "yb_send_sticker":
		// Mixed read/write or interactive tools use neutral checking phrases;
		// the name alone cannot tell which action ran or whether it succeeded.
		return "session_status"
	}

	switch {
	case key == "x_search":
		return "x_search"
	case strings.Contains(key, "memory_search"):
		return "memory_search"
	case strings.Contains(key, "memory_get") || strings.Contains(key, "memory_read"):
		return "memory_get"
	case strings.Contains(key, "web_search") || key == "search" || strings.HasSuffix(key, "_search"):
		return "web_search"
	case strings.Contains(key, "web_fetch") || strings.Contains(key, "http_fetch") || strings.HasSuffix(key, "_fetch"):
		return "web_fetch"
	case key == "bash" || key == "shell" || key == "command_execution" || key == "command" || key == "run" ||
		strings.HasSuffix(key, "__exec") || strings.HasSuffix(key, "__shell"):
		return "exec"
	case key == "read" || strings.HasSuffix(key, "__read"):
		return "read"
	case key == "file_changes" || key == "file_change" || key == "edit" || key == "write" || key == "patch":
		return "apply_patch"
	case strings.Contains(key, "image_generate") || strings.Contains(key, "image_create"):
		return "image_generate"
	case strings.Contains(key, "video_generate") || strings.Contains(key, "video_create"):
		return "video_generate"
	case strings.Contains(key, "music_generate") || strings.Contains(key, "music_create"):
		return "music_generate"
	}
	return key
}

// FillerForTool returns the tool-specific override pool for (lang, tool).
// Returns nil when no override exists — caller falls back to
// FillerContinuation. Unknown lang routes to the English pool.
func FillerForTool(lang, tool string) []string {
	if tool = FillerToolKey(tool); tool == "" {
		return nil
	}
	pools, ok := toolFillers[lang]
	if !ok {
		pools = toolFillers[fallbackLang]
	}
	return applyNameAll(pools[tool])
}
