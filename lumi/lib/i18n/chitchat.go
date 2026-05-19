package i18n

// chitchatInputs holds the per-language exact-match input keywords used by
// the local intent matcher to detect bare social phrases (greeting, farewell,
// thanks). Reply variants for the same Phrase key live in the standard
// phrases map and are accessed via PickIn.
//
// Adding a new chitchat intent: add a Phrase const + entry here for input
// matchers + entry in phrases.go for reply variants + entry in
// ChitchatPhrases() so the intent matcher iterates it.
var chitchatInputs = map[Phrase]map[string][]string{
	PhraseChitchatGreeting: {
		LangVI:   {"chào", "chào lumi", "xin chào", "lumi ơi", "hey lumi"},
		LangEN:   {"hi", "hello", "hi lumi", "hello lumi", "hey", "hey lumi"},
		LangZhCN: {"你好", "你好啊", "嗨", "嘿"},
		LangZhTW: {"你好", "嗨"},
	},
	PhraseChitchatFarewell: {
		LangVI:   {"tạm biệt", "tạm biệt lumi"},
		LangEN:   {"bye", "bye lumi", "goodbye", "see you", "see ya", "later"},
		LangZhCN: {"再见", "拜拜"},
		LangZhTW: {"再見", "拜拜"},
	},
	PhraseChitchatThanks: {
		LangVI:   {"cảm ơn", "cảm ơn lumi"},
		LangEN:   {"thanks", "thank you", "thanks lumi", "thx"},
		LangZhCN: {"谢谢", "谢谢你"},
		LangZhTW: {"謝謝", "謝謝你"},
	},
	PhraseChitchatApology: {
		LangVI:   {"xin lỗi", "tớ xin lỗi", "mình xin lỗi", "lỗi của mình"},
		LangEN:   {"sorry", "i'm sorry", "im sorry", "my bad", "apologies"},
		LangZhCN: {"对不起", "抱歉"},
		LangZhTW: {"對不起", "抱歉"},
	},
	PhraseChitchatCompliment: {
		LangVI:   {"giỏi quá", "giỏi ghê", "xinh quá", "xinh ghê", "dễ thương quá", "đáng yêu quá", "tuyệt vời"},
		LangEN:   {"good job", "good lamp", "good girl", "good boy", "well done", "nice job", "great job", "you're cute", "you're awesome"},
		LangZhCN: {"真棒", "棒棒哒", "好可爱"},
		LangZhTW: {"真棒", "好可愛"},
	},
	PhraseChitchatNevermind: {
		LangVI:   {"thôi", "thôi quên đi", "thôi bỏ đi", "bỏ đi", "không sao"},
		LangEN:   {"never mind", "nevermind", "forget it", "drop it", "no matter"},
		LangZhCN: {"算了"},
		LangZhTW: {"算了"},
	},
	PhraseChitchatPresenceCheck: {
		LangVI:   {"còn đó không", "còn đó hông", "vẫn còn đó chứ", "có nghe không"},
		LangEN:   {"are you there", "are you still there", "you still there", "you there"},
		LangZhCN: {"在吗", "你在吗", "还在吗"},
		LangZhTW: {"在嗎", "你在嗎", "還在嗎"},
	},
}

// InputPhrases returns the per-language exact-match input keywords for the
// chitchat Phrase p. Returns nil when p isn't a chitchat phrase.
func InputPhrases(p Phrase) map[string][]string {
	return chitchatInputs[p]
}

// ChitchatPhrases returns the list of chitchat phrase keys in match order.
// intent.go iterates this so adding a new chitchat intent only needs i18n
// edits (Phrase const + phrases entry + chitchatInputs entry + this list).
func ChitchatPhrases() []Phrase {
	return []Phrase{
		// Specific phrases first — generic greeting/farewell would
		// substring-eat the more specific ones if listed earlier.
		PhraseChitchatPresenceCheck,
		PhraseChitchatApology,
		PhraseChitchatCompliment,
		PhraseChitchatGreeting,
		PhraseChitchatFarewell,
		PhraseChitchatThanks,
		PhraseChitchatNevermind,
	}
}

// chitchatCommandWords are verbs/nouns per language that signal an action
// request, not a social phrase. The intent matcher rejects chitchat match
// when any of these appear in the input ("chào lumi bật đèn" → bật in VN
// command words → fall through to command rules so the LED toggle fires).
var chitchatCommandWords = map[string][]string{
	LangVI: {
		"bật", "tắt", "mở", "đóng", "phát", "dừng", "đổi", "chuyển",
		"chụp", "kể", "đọc", "hát", "hỏi", "tìm", "xem", "nói",
		"to lên", "nhỏ lại", "lớn hơn", "nhỏ hơn", "im lặng",
		"nhạc", "đèn", "ảnh",
	},
	LangEN: {
		"turn", "play", "stop", "switch", "change", "open", "close", "set",
		"show", "take", "tell", "read", "sing", "find", "search", "ask",
		"louder", "softer", "mute", "unmute", "lights", "music", "song",
	},
	LangZhCN: {"开", "关", "播放", "停", "换", "唱", "讲", "找", "拍", "看"},
	LangZhTW: {"開", "關", "播放", "停", "換", "唱", "講", "找", "拍", "看"},
}

// ChitchatCommandWords returns every command word across every supported
// language, flattened. Used by the intent matcher to reject chitchat on any
// command-bearing text regardless of which language the user is speaking.
func ChitchatCommandWords() []string {
	var out []string
	for _, ws := range chitchatCommandWords {
		out = append(out, ws...)
	}
	return out
}

// chitchatWakeWords are name tokens the user prepends before chitchat — the
// wake word itself plus common STT mis-transcriptions ("Lumi" → "Làmi" /
// "Lami" / "Lumy") and attention-call compounds ("Lumi ơi"). Stripped from
// the head of normalized chitchat input so "Lumi xin chào" matches "xin
// chào" and bare "Lumi ơi" → "" → greeting reply path.
//
// Order matters: longest forms first so "lumi ơi xin chào" strips the full
// "lumi ơi" rather than just "lumi".
var chitchatWakeWords = []string{
	// Compound attention-call forms — must come before bare names.
	"lumi ơi", "loomi ơi", "lumy ơi", "luumi ơi", "lami ơi", "làmi ơi", "noah ơi",
	// Bare-name forms.
	"lumi", "loomi", "lumy", "luumi", "lami", "làmi", "noah",
}

// ChitchatWakeWords returns the wake-word list for chitchat input
// normalization, longest forms first so caller can strip the maximal
// leading match (followed by space, comma, punctuation, or end-of-string).
func ChitchatWakeWords() []string {
	return chitchatWakeWords
}
