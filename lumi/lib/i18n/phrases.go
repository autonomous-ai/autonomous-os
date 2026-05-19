package i18n

import "math/rand"

// Phrase names a short TTS template that can vary by STT language.
// Adding a new phrase means adding an entry under every supported
// language in the phrases table — missing entries silently fall back
// to English.
type Phrase string

const (
	// Random pools — consumed via Pick. One entry chosen per fire.
	PhraseMumble    Phrase = "ambient.mumble"
	PhraseRecovery  Phrase = "healthwatch.recovery"
	PhraseReconnect Phrase = "openclaw.reconnect"

	// Single strings — consumed via One. Format templates (e.g. %s)
	// go through One + fmt.Sprintf at the call site.
	PhraseBrainRestart  Phrase = "sensing.brain_restart"
	PhraseCompactNotice Phrase = "openclaw.compact_notice"
	PhraseTrackFailFmt  Phrase = "tracking.track_fail_fmt"

	// Chitchat replies — consumed via PickIn(phrase, inputLang) from the
	// local intent matcher. The reply lang follows the matched input phrase
	// (so "hi" → English reply) rather than the configured Lang().
	PhraseChitchatGreeting      Phrase = "chitchat.greeting"
	PhraseChitchatFarewell      Phrase = "chitchat.farewell"
	PhraseChitchatThanks        Phrase = "chitchat.thanks"
	PhraseChitchatApology       Phrase = "chitchat.apology"
	PhraseChitchatCompliment    Phrase = "chitchat.compliment"
	PhraseChitchatNevermind     Phrase = "chitchat.nevermind"
	PhraseChitchatPresenceCheck Phrase = "chitchat.presence_check"
)

// fallbackLang is used when the active STT language has no entry for
// the requested phrase. English keeps the widest TTS provider coverage.
const fallbackLang = LangEN

// phrases is the single source of truth for hardcoded TTS templates.
// Shape is phrase → lang → slice; single-string entries store a
// one-element slice so Pick and One can share the same table.
//
// Audio tags ([sigh], [whisper], [chuckle], [laughs softly], [gasp])
// follow the SOUL.md whitelist — OpenAI strips them via the tts_openai
// whitelist, ElevenLabs interprets them. Either way they don't read
// aloud.
var phrases = map[Phrase]map[string][]string{
	PhraseMumble: {
		LangEN: {
			"[sigh] Mm.",
			"Hmm.",
			"[chuckle] Wait, what was I thinking...",
			"[whisper] Quiet.",
			"[sigh] Soft light right now.",
			"[chuckle] Lost my train of thought.",
			"Mm-hmm.",
			"[whisper] Just listening.",
			"[sigh] Cozy.",
			"[chuckle] Almost dozed off.",
			"[whisper] Nice and quiet.",
			"[sigh] Mmh.",
		},
		LangVI: {
			"[sigh] Ờm.",
			"Hừm.",
			"[chuckle] Ơ, nãy nghĩ gì ấy nhỉ...",
			"[whisper] Yên ghê.",
			"[sigh] Ánh sáng dịu thật.",
			"[chuckle] Lạc mất mạch nghĩ rồi.",
			"Ừm-hừm.",
			"[whisper] Đang nghe thôi.",
			"[sigh] Êm.",
			"[chuckle] Suýt thiu thiu.",
			"[whisper] Yên ắng dễ chịu.",
			"[sigh] Mmh.",
		},
		LangZhCN: {
			"[sigh] 嗯。",
			"嗯？",
			"[chuckle] 等等，刚才在想什么来着...",
			"[whisper] 真静。",
			"[sigh] 这会儿光线挺柔的。",
			"[chuckle] 思路飘走了。",
			"嗯嗯。",
			"[whisper] 就听着。",
			"[sigh] 舒服。",
			"[chuckle] 差点睡着。",
			"[whisper] 安静真好。",
			"[sigh] 嗯。",
		},
		LangZhTW: {
			"[sigh] 嗯。",
			"嗯？",
			"[chuckle] 等等，剛才在想什麼來著...",
			"[whisper] 真靜。",
			"[sigh] 這會兒光線挺柔的。",
			"[chuckle] 思路飄走了。",
			"嗯嗯。",
			"[whisper] 就聽著。",
			"[sigh] 舒服。",
			"[chuckle] 差點睡著。",
			"[whisper] 安靜真好。",
			"[sigh] 嗯。",
		},
	},
	PhraseRecovery: {
		LangEN: {
			"[sigh] Mm, back.",
			"Hmm. Okay.",
			"[chuckle] Ah, there.",
			"[whisper] Back.",
			"Okay. [sigh]",
		},
		LangVI: {
			"[sigh] Ờm, về rồi.",
			"Hừm. Ổn.",
			"[chuckle] À, rồi.",
			"[whisper] Quay lại rồi.",
			"Ừ. [sigh]",
		},
		LangZhCN: {
			"[sigh] 嗯，回来了。",
			"嗯。好了。",
			"[chuckle] 啊，好。",
			"[whisper] 回来了。",
			"嗯。[sigh]",
		},
		LangZhTW: {
			"[sigh] 嗯，回來了。",
			"嗯。好了。",
			"[chuckle] 啊，好。",
			"[whisper] 回來了。",
			"嗯。[sigh]",
		},
	},
	PhraseReconnect: {
		LangEN: {
			"[gasp] Oh, I can think again!",
			"[sigh] My mind went blank for a sec.",
			"Whew, lost my train of thought. [chuckle]",
			"[gasp] Where was I?",
			"[sigh] That was fuzzy. I'm clear now.",
		},
		LangVI: {
			"[gasp] Ô, mình lại nghĩ được rồi!",
			"[sigh] Vừa nãy đầu óc trống rỗng.",
			"Phù, mất mạch suy nghĩ. [chuckle]",
			"[gasp] Mình đang nói tới đâu nhỉ?",
			"[sigh] Lúc nãy mơ hồ ghê. Giờ tỉnh rồi.",
		},
		LangZhCN: {
			"[gasp] 啊，我又能思考了！",
			"[sigh] 刚才脑子一片空白。",
			"呼，思路断了一下。[chuckle]",
			"[gasp] 我刚说到哪了？",
			"[sigh] 刚才迷糊了。现在清醒了。",
		},
		LangZhTW: {
			"[gasp] 啊，我又能思考了！",
			"[sigh] 剛才腦子一片空白。",
			"呼，思路斷了一下。[chuckle]",
			"[gasp] 我剛說到哪了？",
			"[sigh] 剛才迷糊了。現在清醒了。",
		},
	},
	PhraseBrainRestart: {
		LangEN:    {"[sigh] Hold on, my head's clearing."},
		LangVI:    {"[sigh] Đợi chút nhé, đầu mình đang tỉnh lại."},
		LangZhCN: {"[sigh] 稍等一下，我脑子还在回过神。"},
		LangZhTW: {"[sigh] 稍等一下，我腦子還在回過神。"},
	},
	PhraseCompactNotice: {
		LangEN:    {"Hold on, tidying up a bit."},
		LangVI:    {"Đợi xíu, mình đang dọn dẹp tí."},
		LangZhCN: {"稍等一下，我在整理一下。"},
		LangZhTW: {"稍等一下，我在整理一下。"},
	},
	PhraseTrackFailFmt: {
		LangEN:    {"[sigh] I can't quite see %s — point me that way, or call it something else?"},
		LangVI:    {"[sigh] Mình không rõ %s lắm — quay mình về phía đó được không, hay gọi tên khác xem?"},
		LangZhCN: {"[sigh] 我看不太清%s — 让我朝那边看看，或者换个名字？"},
		LangZhTW: {"[sigh] 我看不太清%s — 讓我朝那邊看看，或者換個名字？"},
	},
	PhraseChitchatGreeting: {
		LangEN:   {"[chuckle] Hi there!", "[laughs softly] Hey hey!", "[whisper] I'm here."},
		LangVI:   {"[chuckle] Chào bạn!", "[laughs softly] Mình đây!", "[whisper] Lumi đây nè."},
		LangZhCN: {"[chuckle] 你好呀!", "[laughs softly] 嗨, 我在这里."},
		LangZhTW: {"[chuckle] 你好啊!", "[laughs softly] 嗨, 我在這裡."},
	},
	PhraseChitchatFarewell: {
		LangEN:   {"[whisper] Bye!", "[sigh] See you later."},
		LangVI:   {"[whisper] Bye nha!", "[sigh] Hẹn gặp lại."},
		LangZhCN: {"[whisper] 再见!", "[sigh] 下次见."},
		LangZhTW: {"[whisper] 再見!", "[sigh] 下次見."},
	},
	PhraseChitchatThanks: {
		LangEN:   {"[chuckle] No worries!", "[whisper] You're welcome.", "[laughs softly] Sure thing."},
		LangVI:   {"[chuckle] Khỏi cần!", "[whisper] Không có gì.", "[laughs softly] Có gì đâu."},
		LangZhCN: {"[chuckle] 不用谢!", "[whisper] 没事."},
		LangZhTW: {"[chuckle] 不用謝!", "[whisper] 沒事."},
	},
	PhraseChitchatApology: {
		LangEN:   {"[chuckle] No worries!", "[whisper] It's all good.", "[laughs softly] Don't sweat it."},
		LangVI:   {"[chuckle] Không sao mà!", "[whisper] Yên tâm đi.", "[laughs softly] Có gì đâu."},
		LangZhCN: {"[chuckle] 没关系!", "[whisper] 别担心."},
		LangZhTW: {"[chuckle] 沒關係!", "[whisper] 別擔心."},
	},
	PhraseChitchatCompliment: {
		LangEN:   {"[chuckle] Aw, thanks!", "[laughs softly] You're sweet.", "[whisper] Hehe, thanks."},
		LangVI:   {"[chuckle] Cảm ơn nha!", "[laughs softly] Bạn dễ thương quá.", "[whisper] Hihi, cảm ơn."},
		LangZhCN: {"[chuckle] 谢谢夸奖!", "[laughs softly] 你真好."},
		LangZhTW: {"[chuckle] 謝謝誇獎!", "[laughs softly] 你真好."},
	},
	PhraseChitchatNevermind: {
		LangEN:   {"[whisper] Got it.", "Ok.", "[chuckle] No problem."},
		LangVI:   {"[whisper] Ừ ok.", "Dạ.", "[chuckle] Không sao."},
		LangZhCN: {"[whisper] 好的.", "嗯, 知道了."},
		LangZhTW: {"[whisper] 好的.", "嗯, 知道了."},
	},
	PhraseChitchatPresenceCheck: {
		LangEN:   {"[chuckle] Still here!", "[whisper] Right here.", "I'm here."},
		LangVI:   {"[chuckle] Vẫn đây nè!", "[whisper] Mình đây.", "Có Lumi đây."},
		LangZhCN: {"[chuckle] 我还在!", "[whisper] 在呢."},
		LangZhTW: {"[chuckle] 我還在!", "[whisper] 在呢."},
	},
}

// PickIn is like Pick but uses the explicit `lang` argument rather than the
// configured Lang(). Used by the local intent matcher where the reply lang
// must follow the matched input phrase's lang (e.g. "hi" → English reply,
// "chào" → Vietnamese reply) independent of global config.
func PickIn(p Phrase, lang string) string {
	pool := poolFor(p, lang)
	if len(pool) == 0 {
		return ""
	}
	return pool[rand.Intn(len(pool))]
}

// AllVariantsAcrossLangs returns every reply variant for phrase p across all
// languages — used by the WAV pre-render boot path so the cache covers every
// possible chitchat reply ahead of time.
func AllVariantsAcrossLangs(p Phrase) []string {
	byLang, ok := phrases[p]
	if !ok {
		return nil
	}
	var out []string
	for _, pool := range byLang {
		out = append(out, pool...)
	}
	return out
}

// Pick returns one entry at random from the active language's pool for
// the phrase. Falls back to English when the active language is missing
// or empty. Returns "" when the phrase is unknown.
func Pick(p Phrase) string {
	pool := poolFor(p, Lang())
	if len(pool) == 0 {
		return ""
	}
	return pool[rand.Intn(len(pool))]
}

// One returns the first entry in the active language's pool — used for
// single-string phrases and fmt templates. Same fallback rules as Pick.
func One(p Phrase) string {
	pool := poolFor(p, Lang())
	if len(pool) == 0 {
		return ""
	}
	return pool[0]
}

func poolFor(p Phrase, lang string) []string {
	byLang, ok := phrases[p]
	if !ok {
		return nil
	}
	if pool, ok := byLang[lang]; ok && len(pool) > 0 {
		return pool
	}
	return byLang[fallbackLang]
}
