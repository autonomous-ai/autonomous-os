import { C, SectionCard } from "./shared";

export function LanguageSection({
  active, sttLanguage, setSttLanguage,
}: {
  active: boolean;
  sttLanguage: string;
  setSttLanguage: (v: string) => void;
}) {
  return (
    <SectionCard id="language" title="Language" active={active}>
      <div style={{ fontSize: 11, color: C.textDim, marginBottom: 10 }}>
        Pick the language your device listens for. You can change this anytime from the Edit page.
      </div>
      <div style={{ marginBottom: 4 }}>
        <label htmlFor="stt_language" style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>
          Language
        </label>
        <select
          id="stt_language"
          value={sttLanguage}
          onChange={(e) => setSttLanguage(e.target.value)}
          style={{
            width: "100%", boxSizing: "border-box",
            background: C.surface, border: `1px solid ${C.border}`,
            borderRadius: 7, padding: "8px 11px",
            fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
          }}
        >
          <option value="">Auto (default)</option>
          <option value="en">English</option>
          <option value="vi">Vietnamese</option>
          <option value="zh-CN">Chinese (Simplified)</option>
          <option value="zh-TW">Chinese (Traditional)</option>
        </select>
      </div>
    </SectionCard>
  );
}
