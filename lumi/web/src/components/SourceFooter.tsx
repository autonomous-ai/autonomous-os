// GPL v3 §6 compliance: lelamp/ ships GPLv3 source baked into the board image,
// so buyers must be told where to fetch corresponding source. Public repo URL
// satisfies the "written offer" alternative. Rendered globally (App.tsx)
// so every page (setup, login, monitor, edit, gw-config) surfaces the link.
import { C } from "@/components/setup/shared";

export function SourceFooter() {
  return (
    <a
      href="https://github.com/autonomous-ai/ai-lamp-lumi"
      target="_blank"
      rel="noopener noreferrer"
      style={{
        position: "fixed", right: 8, bottom: 6,
        fontSize: 10, color: C.textMuted,
        textDecoration: "none", opacity: 0.7,
        padding: "2px 6px", borderRadius: 4,
        background: "transparent",
        pointerEvents: "auto", zIndex: 1,
        fontFamily: "ui-monospace, monospace",
      }}
      title="Source code (GPL v3)"
    >
      ⌥ github.com/autonomous-ai/ai-lamp-lumi
    </a>
  );
}
