import { useState } from "react";
import { FileText, Download } from "lucide-react";
import { agentFileUrl } from "@/lib/api";

// Files the agent produced, surfaced under its reply.
//
// The chat can send a file INTO a turn, but an agent that makes one can only
// name it: ask for a photo and the reply carries an absolute device path
// (`/root/.openclaw/media/hal-snapshots/snap_*.jpg` — see skills/camera), which
// is unusable in a browser. So the reply text is scanned for such paths and each
// one is rendered as an image or a download chip pointing at
// GET /api/agent/file.
//
// Detection is CLIENT-side on purpose: it costs no change to the turn pipeline,
// and it applies to conversations already in localStorage, which a server-side
// scan at turn-end could never reach. Enforcement stays server-side — the roots
// below only stop the UI from firing requests for paths that could never be
// served; the backend re-validates every one of them.

// Roots that GET /api/agent/file will serve (handler_file.go
// defaultAgentFileRoots). Duplicated here as a filter, never as a permission.
const ROOTS = String.raw`(?:/root/\.[a-z0-9_-]+/(?:media|workspace)|/tmp)`;

// Served extensions, split by how they are shown.
const IMAGE_EXT = ["jpg", "jpeg", "png", "gif", "webp"];
const OTHER_EXT = ["pdf", "txt", "md", "csv", "wav", "mp3", "mp4", "webm"];

// One absolute path under a served root, ending in a served extension. The
// character class stops at whatever normally terminates a path in prose or
// markdown — whitespace, quotes, brackets, backticks.
const FILE_RE = new RegExp(
  `${ROOTS}/[^\\s"'\`)<>\\]]+\\.(${[...IMAGE_EXT, ...OTHER_EXT].join("|")})\\b`,
  "gi",
);

/** The parts of a tool chip that can name a file. Structural on purpose — the
 *  full ToolChip type lives in ChatSection and this needs three fields of it. */
interface FileBearingTool {
  args?: Record<string, unknown>;
  detail?: string;
  result?: string;
}

// Device paths named anywhere in the turn, de-duplicated, in the order found.
//
// The reply text alone is NOT enough, which the first device test made obvious:
// asked to send a photo, the agent called its `message` tool with
// `{"action":"send","media":"/root/.openclaw/media/…jpg"}` and its spoken reply
// mentioned no path at all — from the chat's side the file was invisible. Tool
// args carry it (the server logs them untruncated in `detail.args`; only the
// chip's DISPLAY is shortened), and a `curl /camera/snapshot` puts it in the
// tool result instead. All three are searched.
function extractAgentFiles(text: string, tools?: FileBearingTool[]): string[] {
  const haystacks: string[] = [text || ""];
  for (const t of tools ?? []) {
    if (t.args) {
      try {
        haystacks.push(JSON.stringify(t.args));
      } catch { /* circular/unserializable args — nothing to scan */ }
    }
    if (t.detail) haystacks.push(t.detail);
    if (t.result) haystacks.push(t.result);
  }

  const seen = new Set<string>();
  for (const hay of haystacks) {
    for (const m of hay.matchAll(FILE_RE)) {
      // Trailing punctuation belongs to the sentence, not the filename.
      seen.add(m[0].replace(/[.,;:]+$/, ""));
    }
  }
  return [...seen];
}

export function AgentFiles({ text, tools }: { text: string; tools?: FileBearingTool[] }) {
  const paths = extractAgentFiles(text, tools);
  if (paths.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
      {paths.map((p) => <AgentFile key={p} path={p} />)}
    </div>
  );
}

function AgentFile({ path }: { path: string }) {
  // A path can be named but gone (temp file cleaned up), or refused by the
  // backend. Either way the attachment disappears and the path stays readable
  // as text in the reply above — nothing breaks, nothing shouts an error.
  const [failed, setFailed] = useState(false);
  if (failed) return null;

  const name = path.slice(path.lastIndexOf("/") + 1);
  const ext = name.slice(name.lastIndexOf(".") + 1).toLowerCase();
  const url = agentFileUrl(path);

  if (IMAGE_EXT.includes(ext)) {
    return (
      <a href={url} target="_blank" rel="noreferrer noopener" title={path}>
        <img
          src={url}
          alt={name}
          onError={() => setFailed(true)}
          style={{
            maxWidth: "100%", maxHeight: 320, borderRadius: 10, display: "block",
            border: "1px solid var(--lm-border)",
          }}
        />
      </a>
    );
  }

  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer noopener"
      download={name}
      title={path}
      style={{
        display: "inline-flex", alignItems: "center", gap: 9, alignSelf: "flex-start",
        maxWidth: "100%", padding: "8px 11px", borderRadius: 10,
        background: "var(--lm-card)", border: "1px solid var(--lm-border)",
        color: "var(--lm-text)", textDecoration: "none",
      }}
    >
      <FileText size={15} style={{ color: "var(--lm-amber)", flexShrink: 0, alignSelf: "flex-start", marginTop: 1 }} />
      <span style={{
        fontSize: 12.5, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
      }}>{name}</span>
      <Download size={13} style={{ color: "var(--lm-text-dim)", flexShrink: 0 }} />
    </a>
  );
}
