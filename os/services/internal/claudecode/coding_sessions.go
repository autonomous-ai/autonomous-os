package claudecode

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// Coding-session discovery for the Telegram remote-coding feature.
//
// The claude CLI stores every session as a JSONL transcript under
// ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl. The device runs claude
// as root, so the tree lives at /root/.claude/projects. This file reads that
// tree so Telegram can list resumable sessions and continue any of them (see
// telegram_coding.go). Sessions are cwd-scoped: `claude --resume <uuid>` only
// finds the session when run from the folder it was created in — so each
// session carries its real cwd, recovered from the transcript (NOT decoded from
// the directory name, whose /→- encoding is lossy for folders containing '-').

const (
	// claudeProjectsDirDefault is the on-device session store. Overridable via
	// the claudeProjectsDirPath test seam.
	claudeProjectsDirDefault = "/root/.claude/projects"

	// transcriptScanLimit bounds how many bytes of a transcript are read while
	// recovering its cwd + summary (both live in the first records).
	transcriptScanLimit = 256 * 1024
)

// codingSession is one resumable claude session discovered on disk.
type codingSession struct {
	Folder    string    // real cwd — the dir `claude --resume` must run in
	SessionID string    // session uuid
	Modified  time.Time // transcript mtime — recency for listing / newest-per-folder
	Summary   string    // short human label (session summary or first user line)
}

// claudeProjectsDir returns the session store path (test seam or default).
func (s *ClaudeCodeService) claudeProjectsDir() string {
	if s.claudeProjectsDirPath != "" {
		return s.claudeProjectsDirPath
	}
	return claudeProjectsDirDefault
}

// allCodingSessions returns every discovered session, most-recently-modified
// first. Transcripts whose cwd can't be recovered are skipped (they can't be
// resumed reliably anyway).
func (s *ClaudeCodeService) allCodingSessions() []codingSession {
	dir := s.claudeProjectsDir()
	projects, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var out []codingSession
	for _, p := range projects {
		if !p.IsDir() {
			continue
		}
		projDir := filepath.Join(dir, p.Name())
		files, err := os.ReadDir(projDir)
		if err != nil {
			continue
		}
		for _, f := range files {
			if f.IsDir() || !strings.HasSuffix(f.Name(), ".jsonl") {
				continue
			}
			info, err := f.Info()
			if err != nil {
				continue
			}
			folder, summary := readTranscriptMeta(filepath.Join(projDir, f.Name()))
			if folder == "" {
				continue
			}
			out = append(out, codingSession{
				Folder:    folder,
				SessionID: strings.TrimSuffix(f.Name(), ".jsonl"),
				Modified:  info.ModTime(),
				Summary:   summary,
			})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Modified.After(out[j].Modified) })
	return out
}

// codingFolders returns the NEWEST session per folder, most-recent folder first
// — the default /sessions view (one line per project).
func (s *ClaudeCodeService) codingFolders() []codingSession {
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

// folderSessions returns all sessions in one folder, newest first.
func (s *ClaudeCodeService) folderSessions(folder string) []codingSession {
	folder = normalizeFolder(folder)
	var out []codingSession
	for _, cs := range s.allCodingSessions() {
		if cs.Folder == folder {
			out = append(out, cs)
		}
	}
	return out
}

// latestSessionForFolder returns the newest session in folder (ok=false if none).
func (s *ClaudeCodeService) latestSessionForFolder(folder string) (codingSession, bool) {
	sessions := s.folderSessions(folder)
	if len(sessions) == 0 {
		return codingSession{}, false
	}
	return sessions[0], true
}

// readTranscriptMeta recovers a transcript's cwd and a short summary. cwd is
// taken from the first record that carries one; summary prefers an explicit
// {"type":"summary"} record, else the first user message text. Reads at most
// transcriptScanLimit bytes.
func readTranscriptMeta(path string) (folder, summary string) {
	f, err := os.Open(path)
	if err != nil {
		return "", ""
	}
	defer f.Close()

	sc := bufio.NewScanner(io.LimitReader(f, transcriptScanLimit))
	sc.Buffer(make([]byte, 0, 64*1024), transcriptScanLimit)
	var firstUser string
	for sc.Scan() {
		var rec struct {
			Type    string          `json:"type"`
			Cwd     string          `json:"cwd"`
			Summary string          `json:"summary"`
			Message json.RawMessage `json:"message"`
		}
		if json.Unmarshal(sc.Bytes(), &rec) != nil {
			continue
		}
		if folder == "" && rec.Cwd != "" {
			folder = rec.Cwd
		}
		if summary == "" && rec.Type == "summary" && rec.Summary != "" {
			summary = rec.Summary
		}
		if firstUser == "" && rec.Type == "user" {
			// Skip the synthetic <environment_context>/<system-reminder> blocks
			// the CLI injects as the first "user" message — the real prompt is the
			// first non-injected user text.
			if txt := userRecordText(rec.Message); txt != "" && !isInjectedContext(txt) {
				firstUser = txt
			}
		}
		if folder != "" && summary != "" {
			break
		}
	}
	if summary == "" {
		summary = firstUser
	}
	return folder, truncRunes(oneLine(summary), 90)
}

// userRecordText pulls plain text out of a transcript user record's message,
// whose content is either a string or an array of {type:"text",text} blocks.
func userRecordText(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var m struct {
		Content json.RawMessage `json:"content"`
	}
	if json.Unmarshal(raw, &m) != nil {
		return ""
	}
	var str string
	if json.Unmarshal(m.Content, &str) == nil {
		return str
	}
	var blocks []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if json.Unmarshal(m.Content, &blocks) == nil {
		parts := make([]string, 0, len(blocks))
		for _, b := range blocks {
			if b.Type == "text" && b.Text != "" {
				parts = append(parts, b.Text)
			}
		}
		return strings.Join(parts, " ")
	}
	return ""
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
