// Lightweight UI i18n for the web frontend, mirroring the Go backend's
// os/services/lib/i18n conventions:
//
//   - Same BCP-47 language constants (en / vi / zh-CN / zh-TW) plus the
//     alias codes (zh, zh-Hans, zh-Hant) the STT config accepts, normalised
//     onto the canonical codes for lookups — see lib/i18n/lang.go.
//   - A single active language, set once from the device config's
//     `stt_language` field (the same source Go's i18n.Lang() reads from
//     config.STTLanguage). setLanguage() ⇔ Go SetConfig; getLanguage() ⇔ Lang().
//   - Lookup falls back to English when a string is missing for the active
//     language — Go uses the same fallbackLang = LangEN rule.
//
// This is intentionally hand-rolled (no i18next dependency): the frontend has
// no other i18n today, the string set is small, and matching the Go module's
// shape keeps the two consistent for anyone working across both.

import { useSyncExternalStore } from "react";

// BCP-47 codes. Canonical set mirrors lib/i18n/lang.go. Aliases are accepted
// on input and normalised onto the canonical codes.
export const LANG = {
  EN: "en",
  VI: "vi",
  ZH_CN: "zh-CN",
  ZH_TW: "zh-TW",
} as const;

export type Lang = (typeof LANG)[keyof typeof LANG];

const FALLBACK_LANG: Lang = LANG.EN;

// Normalise an STT-config language code (which may be an alias like "zh",
// "zh-Hans", "zh-Hant", or carry a region like "en-US") onto one of the
// canonical content languages. Mirrors the alias handling documented in
// lib/i18n/lang.go. Unknown codes fall back to English.
export function normalizeLang(code: string | undefined | null): Lang {
  if (!code) return FALLBACK_LANG;
  const c = code.toLowerCase();
  if (c === "vi" || c.startsWith("vi-")) return LANG.VI;
  if (c === "zh-tw" || c === "zh-hant" || c.startsWith("zh-hant-") || c === "zh-hk" || c === "zh-mo") return LANG.ZH_TW;
  if (c === "zh" || c === "zh-cn" || c === "zh-hans" || c.startsWith("zh-hans-") || c.startsWith("zh-")) return LANG.ZH_CN;
  if (c === "en" || c.startsWith("en-")) return LANG.EN;
  return FALLBACK_LANG;
}

// ── Active language store ───────────────────────────────────────────────────
// A tiny observable so React components re-render when the language resolves
// from the (async) device config. Go's i18n is a process singleton; the web
// equivalent needs reactivity, hence useSyncExternalStore below.

let active: Lang = FALLBACK_LANG;
const listeners = new Set<() => void>();

// setLanguage wires the active language, typically from the device config's
// stt_language right after it loads. Accepts raw/alias codes; normalises.
// Mirrors Go i18n.SetConfig. No-op when the resolved language is unchanged.
export function setLanguage(code: string | undefined | null): void {
  const next = normalizeLang(code);
  if (next === active) return;
  active = next;
  for (const l of listeners) l();
}

// getLanguage returns the active canonical language. Mirrors Go i18n.Lang(),
// except it returns the English fallback rather than "" before it's set.
export function getLanguage(): Lang {
  return active;
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

// ── String catalogue ────────────────────────────────────────────────────────
// Keyed by a dotted id, then by language. English is mandatory (it's the
// fallback); other languages are optional and fall back to English per key.
// Keep keys scoped by feature (chat.*) so the catalogue stays navigable.

type Catalogue = Record<string, Partial<Record<Lang, string>> & { en: string }>;

const strings: Catalogue = {
  // Empty chat screen
  "chat.empty.title": {
    en: "Chat with Assistant",
    vi: "Trò chuyện với Assistant",
    "zh-CN": "与助手聊天",
    "zh-TW": "與助手聊天",
  },
  "chat.empty.subtitle": {
    en: "Ask anything, or try one of these",
    vi: "Hỏi bất cứ điều gì, hoặc thử một gợi ý",
    "zh-CN": "随便问，或试试以下建议",
    "zh-TW": "隨便問，或試試以下建議",
  },

  // Suggestion chips
  "chat.suggest.music": {
    en: "Play a relaxing song",
    vi: "Mở một bài nhạc thư giãn",
    "zh-CN": "播放一首轻松的歌",
    "zh-TW": "播放一首輕鬆的歌",
  },
  "chat.suggest.howAreYou": {
    en: "How are you today?",
    vi: "Hôm nay bạn thế nào?",
    "zh-CN": "你今天怎么样？",
    "zh-TW": "你今天好嗎？",
  },
  "chat.suggest.warmLight": {
    en: "Set a warm light mood",
    vi: "Chỉnh ánh sáng ấm áp",
    "zh-CN": "调成温暖的灯光",
    "zh-TW": "調成溫暖的燈光",
  },
  "chat.suggest.whatCanYouDo": {
    en: "What can you do?",
    vi: "Bạn làm được gì?",
    "zh-CN": "你能做什么？",
    "zh-TW": "你能做什麼？",
  },

  // Assistant presence (top bar status line)
  "chat.status.thinking": {
    en: "Assistant is thinking…",
    vi: "Assistant đang suy nghĩ…",
    "zh-CN": "助手正在思考…",
    "zh-TW": "助手正在思考…",
  },
  "chat.status.online": {
    en: "Assistant · online",
    vi: "Assistant · trực tuyến",
    "zh-CN": "助手 · 在线",
    "zh-TW": "助手 · 在線",
  },

  // History panel — relative timestamps. {n} is substituted with the count.
  "chat.time.now": {
    en: "now",
    vi: "vừa xong",
    "zh-CN": "刚刚",
    "zh-TW": "剛剛",
  },
  "chat.time.minutes": {
    en: "{n}m",
    vi: "{n} phút",
    "zh-CN": "{n}分钟前",
    "zh-TW": "{n}分鐘前",
  },
  "chat.time.hours": {
    en: "{n}h",
    vi: "{n} giờ",
    "zh-CN": "{n}小时前",
    "zh-TW": "{n}小時前",
  },
  "chat.time.yesterday": {
    en: "yesterday",
    vi: "hôm qua",
    "zh-CN": "昨天",
    "zh-TW": "昨天",
  },
  "chat.time.days": {
    en: "{n}d",
    vi: "{n} ngày",
    "zh-CN": "{n}天前",
    "zh-TW": "{n}天前",
  },
};

// t looks up a string by key in the active language, falling back to English
// when the key has no entry for that language (mirrors Go's fallbackLang).
// An unknown key returns the key itself so missing strings are visible rather
// than blank. `params` substitutes `{name}` placeholders (e.g. {n}).
export function t(key: string, params?: Record<string, string | number>, lang?: Lang): string {
  const entry = strings[key];
  if (!entry) return key;
  const l = lang ?? active;
  let out = entry[l] ?? entry.en;
  if (params) {
    for (const k in params) out = out.replace(`{${k}}`, String(params[k]));
  }
  return out;
}

// useT subscribes a component to language changes and returns a bound t() that
// re-renders when the active language resolves from the device config.
export function useT(): (key: string, params?: Record<string, string | number>) => string {
  const lang = useSyncExternalStore(subscribe, getLanguage, getLanguage);
  return (key, params) => t(key, params, lang);
}
