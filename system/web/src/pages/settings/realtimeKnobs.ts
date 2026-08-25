// Per-provider realtime knobs, kept out of RealtimeSection.tsx so that file only
// exports components (react-refresh/only-export-components). Keep these lists in
// sync with system/server/config/realtime.go (ValidateRealtimeKnobs) and the HAL
// enums.
//
// pipecat and cascaded are the odd ones out: both drive the LLM half of the turn
// from the STT transcript rather than being audio-native, so neither has a voice
// or reasoning knob.
export const VOICES: Record<string, string[]> = {
  gemini: ["Puck", "Charon", "Kore", "Fenrir", "Aoede"],
  openai: ["alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"],
  qwen: ["Cherry", "Serena", "Ethan", "Chelsie"],
  pipecat: [],
  cascaded: [],
};

// Reasoning depth = cost knob. First entry (cheapest) is the default.
// qwen realtime, pipecat and cascaded have no reasoning knob → empty list hides
// the selector.
export const REASONING: Record<string, string[]> = {
  gemini: ["MINIMAL", "LOW", "MEDIUM", "HIGH"],
  openai: ["minimal", "low", "medium", "high", "xhigh"],
  qwen: [],
  pipecat: [],
  cascaded: [],
};

// A provider only accepts the knobs it actually has. The server REJECTS an
// unsupported knob, so the save payload must be filtered by these — hiding the
// selector is not enough, since the state keeps whatever the previous provider
// left behind.
export const providerHasVoice = (p: string): boolean => (VOICES[p] ?? []).length > 0;
export const providerHasReasoning = (p: string): boolean => (REASONING[p] ?? []).length > 0;
