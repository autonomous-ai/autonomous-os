package codex

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// Coding-session (codex thread) discovery for the Telegram remote-coding
// feature. The codex CLI stores every session as a "rollout" JSONL transcript
// under ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl (root ⇒
// /root/.codex/sessions). The first line is a `session_meta` record carrying
// the thread id (`payload.id`) and the working dir (`payload.cwd`). Unlike
// claude, codex resumes by thread id GLOBALLY — `codex exec --cd <dir> resume
// <id>` sets the cwd independently and does NOT require it to match the
// original (device-verified: resuming an old thread echoes its id back). This
// file lists resumable threads so Telegram can continue any of them
// (telegram_coding.go).

const (
	// codexSessionsDirDefault is the on-device rollout store. Overridable via
	// the codexSessionsDirPath test seam.
	codexSessionsDirDefault = "/root/.codex/sessions"

	// rolloutMetaScanLimit bounds how many bytes of a rollout are read while
	// recovering its thread id / cwd / summary (all live in the first records).
	rolloutMetaScanLimit = 256 * 1024
)

// codingSession is one resumable codex thread discovered on disk.
type codingSession struct {
	Folder    string    // cwd recorded in the rollout — passed to `codex exec --cd`
	SessionID string    // codex thread id (`codex exec resume <id>`)
	Modified  time.Time // rollout mtime — recency for listing / newest-per-folder
	Summary   string    // short human label (first user message)
}

// codexSessionsDir returns the rollout store path (test seam or default).
func (s *CodexService) codexSessionsDir() string {
	if s.codexSessionsDirPath != "" {
		return s.codexSessionsDirPath
	}
	return codexSessionsDirDefault
}

// allCodingSessions returns every discovered thread, most-recently-modified
// first, deduped by thread id (codex may write more than one rollout file for a
// resumed thread — keep the newest). Rollouts whose thread id or cwd can't be
// recovered are skipped.
func (s *CodexService) allCodingSessions() []codingSession {
	root := s.codexSessionsDir()
	byThread := map[string]codingSession{}
	_ = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil // unreadable subtree — skip, keep walking
		}
		if d.IsDir() || !strings.HasPrefix(d.Name(), "rollout-") || !strings.HasSuffix(d.Name(), ".jsonl") {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return nil
		}
		threadID, folder, summary := readRolloutMeta(path)
		if threadID == "" || folder == "" {
			return nil
		}
		cur, ok := byThread[threadID]
		if !ok || info.ModTime().After(cur.Modified) {
			byThread[threadID] = codingSession{
				Folder:    folder,
				SessionID: threadID,
				Modified:  info.ModTime(),
				Summary:   summary,
			}
		}
		return nil
	})
	out := make([]codingSession, 0, len(byThread))
	for _, cs := range byThread {
		out = append(out, cs)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Modified.After(out[j].Modified) })
	return out
}

// codingFolders returns the NEWEST thread per folder, most-recent folder first
// — the default /sessions view (one line per project).
func (s *CodexService) codingFolders() []codingSession {
	all := s.allCodingSessions()
	seen := map[string]bool{}
	var out []codingSession
	for _, cs := range all { // already newest-first, so the first hit per folder wins
		if seen[cs.Folder] {
			continue
		}
		seen[cs.Folder] = true
		out = append(out, cs)
	}
	return out
}

// folderSessions returns all threads in one folder, newest first.
func (s *CodexService) folderSessions(folder string) []codingSession {
	folder = normalizeFolder(folder)
	var out []codingSession
	for _, cs := range s.allCodingSessions() {
		if cs.Folder == folder {
			out = append(out, cs)
		}
	}
	return out
}

// latestSessionForFolder returns the newest thread in folder (ok=false if none).
func (s *CodexService) latestSessionForFolder(folder string) (codingSession, bool) {
	sessions := s.folderSessions(folder)
	if len(sessions) == 0 {
		return codingSession{}, false
	}
	return sessions[0], true
}

// readRolloutMeta recovers a rollout's thread id, cwd and a short summary. The
// thread id + cwd come from the first `session_meta` record; the summary is the
// first user message text. Reads at most rolloutMetaScanLimit bytes.
func readRolloutMeta(path string) (threadID, folder, summary string) {
	f, err := os.Open(path)
	if err != nil {
		return "", "", ""
	}
	defer f.Close()

	sc := bufio.NewScanner(io.LimitReader(f, rolloutMetaScanLimit))
	sc.Buffer(make([]byte, 0, 64*1024), rolloutMetaScanLimit)
	for sc.Scan() {
		var rec struct {
			Type    string          `json:"type"`
			Payload json.RawMessage `json:"payload"`
		}
		if json.Unmarshal(sc.Bytes(), &rec) != nil {
			continue
		}
		switch rec.Type {
		case "session_meta":
			var m struct {
				ID  string `json:"id"`
				Cwd string `json:"cwd"`
			}
			if json.Unmarshal(rec.Payload, &m) == nil {
				threadID, folder = m.ID, m.Cwd
			}
		case "response_item":
			// Skip the synthetic <environment_context> block codex injects as the
			// first "user" message — the real prompt is the first non-injected one.
			if summary == "" {
				if txt := rolloutUserText(rec.Payload); txt != "" && !isInjectedContext(txt) {
					summary = txt
				}
			}
		}
		if threadID != "" && folder != "" && summary != "" {
			break
		}
	}
	return threadID, folder, truncRunes(oneLine(summary), 90)
}

// rolloutUserText returns the text of a response_item ONLY when it is a user
// message (role == "user"), whose content is an array of {type:input_text,text}
// blocks. Non-user items (developer/assistant/tool) yield "".
func rolloutUserText(raw json.RawMessage) string {
	var p struct {
		Type    string `json:"type"`
		Role    string `json:"role"`
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
	}
	if json.Unmarshal(raw, &p) != nil || p.Type != "message" || p.Role != "user" {
		return ""
	}
	parts := make([]string, 0, len(p.Content))
	for _, c := range p.Content {
		if c.Text != "" {
			parts = append(parts, c.Text)
		}
	}
	return strings.Join(parts, " ")
}

// normalizeFolder cleans a user-supplied path: trims quotes/space, expands a
// leading ~ to /root, makes it absolute (relative paths resolve under /root)
// and drops any trailing slash.
func normalizeFolder(p string) string {
	p = strings.TrimSpace(p)
	p = strings.Trim(p, `"'`)
	if p == "" {
		return ""
	}
	switch {
	case p == "~":
		p = "/root"
	case strings.HasPrefix(p, "~/"):
		p = filepath.Join("/root", p[2:])
	case !filepath.IsAbs(p):
		p = filepath.Join("/root", p)
	}
	return filepath.Clean(p)
}

// oneLine collapses whitespace/newlines into single spaces for a compact label.
func oneLine(s string) string {
	return strings.Join(strings.Fields(s), " ")
}

// isInjectedContext reports whether a "user" message is actually a synthetic
// context block the CLI prepends before the real prompt (environment info,
// system reminders, IDE/command wrappers) — not something the human typed. Used
// to skip it when picking a session summary.
func isInjectedContext(s string) bool {
	t := strings.TrimSpace(s)
	if t == "" {
		return true
	}
	if !strings.HasPrefix(t, "<") {
		return false
	}
	for _, tag := range []string{
		"<environment_context", "<system-reminder", "<user_instructions",
		"<command-name", "<local-command", "<ide_", "<system>",
	} {
		if strings.HasPrefix(t, tag) {
			return true
		}
	}
	return false
}

// humanizeAgo renders how long ago t was, for session listings.
func humanizeAgo(t time.Time) string {
	d := time.Since(t)
	switch {
	case d < time.Minute:
		return "just now"
	case d < time.Hour:
		return fmt.Sprintf("%dm ago", int(d.Minutes()))
	case d < 24*time.Hour:
		return fmt.Sprintf("%dh ago", int(d.Hours()))
	default:
		return fmt.Sprintf("%dd ago", int(d.Hours()/24))
	}
}
