package migratepersona

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

// baseMigrator holds the shared state and every helper both directions reuse
type baseMigrator struct {
	opts  Options
	items []ItemResult
}

// ── REPORT──
// mode
func (b *baseMigrator) mode() string {
	if b.opts.Execute {
		return "execute"
	}
	return "dry-run"
}

// record appends one item outcome to the report.
func (b *baseMigrator) record(kind, source, destination, status, reason string, details map[string]any) {
	b.items = append(b.items, ItemResult{
		Kind:        kind,
		Source:      source,
		Destination: destination,
		Status:      status,
		Reason:      reason,
		Details:     details,
	})
}

// report assembles the final Report with a per-status summary.
func (b *baseMigrator) report(dir Direction) *Report {
	summary := map[string]int{
		StatusMigrated: 0,
		StatusSkipped:  0,
		StatusConflict: 0,
		StatusError:    0,
	}
	for _, it := range b.items {
		summary[it.Status]++
	}
	return &Report{
		Direction: string(dir),
		Mode:      b.mode(),
		Items:     b.items,
		Summary:   summary,
	}
}

// ── File operations ──

// backup copies an existing destination aside before it is overwritten.
func (b *baseMigrator) backup(path string) (string, error) {
	if _, err := os.Stat(path); err != nil {
		return "", nil // nothing to back up
	}
	dst := fmt.Sprintf("%s.bak-%d", path, time.Now().UnixNano())
	data, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("read for backup: %w", err)
	}
	if err := os.WriteFile(dst, data, 0o644); err != nil {
		return "", fmt.Errorf("write backup: %w", err)
	}
	return dst, nil
}

// copyPersona migrates a single persona file (SOUL.md) with a brand transform.
// Whole-file copy — no entry merging.
//   - missing source            → skipped
//   - dest equals transformed    → skipped ("already matches")
//   - dest exists, !overwrite    → conflict
//   - otherwise                  → backup (if any) + write   (execute)
//     or "would copy"            (dry-run)
func (b *baseMigrator) copyPersona(kind, source, destination string, transform func(string) string) {
	raw, err := os.ReadFile(source)
	if err != nil {
		b.record(kind, source, destination, StatusSkipped, "source file not found", nil)
		return
	}
	content := transform(string(raw))

	if existing, err := os.ReadFile(destination); err == nil {
		if string(existing) == content {
			b.record(kind, source, destination, StatusSkipped, "target already matches source", nil)
			return
		}
		if !b.opts.Overwrite {
			b.record(kind, source, destination, StatusConflict, "target exists and overwrite is disabled", nil)
			return
		}
	}

	if !b.opts.Execute {
		b.record(kind, source, destination, StatusMigrated, "would copy", nil)
		return
	}

	details := map[string]any{}
	if bak, err := b.backup(destination); err != nil {
		b.record(kind, source, destination, StatusError, "backup failed: "+err.Error(), nil)
		return
	} else if bak != "" {
		details["backup"] = bak
	}
	if err := os.MkdirAll(filepath.Dir(destination), 0o755); err != nil {
		b.record(kind, source, destination, StatusError, "create dest dir: "+err.Error(), nil)
		return
	}
	if err := os.WriteFile(destination, []byte(content), 0o644); err != nil {
		b.record(kind, source, destination, StatusError, "write: "+err.Error(), nil)
		return
	}
	b.record(kind, source, destination, StatusMigrated, "", details)
}

// mergeMemory parses, transforms, and merges entries from sources into the destination,
// deduping and enforcing a char limit. dstFormat controls the output format.
// Idempotent; kind/primarySource label the report; extra sources noted in details.
func (b *baseMigrator) mergeMemory(kind string, sources []string, primarySource, destination string, limit int, dstFormat entryFormat, transform func(string) string) {
	var incoming []string
	var usedSources []string
	for _, src := range sources {
		raw, err := os.ReadFile(src)
		if err != nil {
			continue
		}
		usedSources = append(usedSources, src)
		for _, e := range parseEntriesText(string(raw)) {
			incoming = append(incoming, transform(e))
		}
	}

	if len(usedSources) == 0 {
		b.record(kind, primarySource, destination, StatusSkipped, "source file not found", nil)
		return
	}
	if len(incoming) == 0 {
		b.record(kind, primarySource, destination, StatusSkipped, "no importable entries found", nil)
		return
	}

	existing := parseEntries(destination)
	merged, stats, overflowed := mergeEntries(existing, incoming, limit)

	details := map[string]any{
		"existing_entries":   stats.existing,
		"added_entries":      stats.added,
		"duplicate_entries":  stats.duplicates,
		"overflowed_entries": stats.overflowed,
		"char_limit":         limit,
	}
	if len(usedSources) > 1 {
		details["sources"] = usedSources
	}

	if !b.opts.Execute {
		b.record(kind, primarySource, destination, StatusMigrated, "would merge entries", details)
		return
	}
	if stats.added == 0 && len(overflowed) == 0 {
		b.record(kind, primarySource, destination, StatusSkipped, "no new entries to import", details)
		return
	}

	if bak, err := b.backup(destination); err != nil {
		b.record(kind, primarySource, destination, StatusError, "backup failed: "+err.Error(), nil)
		return
	} else if bak != "" {
		details["backup"] = bak
	}
	if err := os.MkdirAll(filepath.Dir(destination), 0o755); err != nil {
		b.record(kind, primarySource, destination, StatusError, "create dest dir: "+err.Error(), nil)
		return
	}
	if err := os.WriteFile(destination, []byte(dstFormat.serialize(merged)), 0o644); err != nil {
		b.record(kind, primarySource, destination, StatusError, "write: "+err.Error(), nil)
		return
	}
	b.record(kind, primarySource, destination, StatusMigrated, "", details)
}

// Memory entry utils: Hermes delimiter, markdown extraction, dedupe, char limit.
// entryDelimiter matches Hermes memory file separator.
const entryDelimiter = "\n§\n"

var (
	reWhitespace   = regexp.MustCompile(`\s+`)
	reHeading      = regexp.MustCompile(`^(#{1,6})\s+(.*\S)\s*$`)
	reBullet       = regexp.MustCompile(`^\s*(?:[-*]|\d+\.)\s+(.*\S)\s*$`)
	reMemFileNames = regexp.MustCompile(`(?i)\b(MEMORY|USER|SOUL|AGENTS|TOOLS|IDENTITY)\.md\b`)
)

// charLen counts unicode code points, matching Python len() on str so the char
// limits behave identically across implementations.
func charLen(s string) int { return utf8.RuneCountInString(s) }

// normalizeText collapses whitespace runs to single spaces and trims; used as
// the dedupe key for entries.
func normalizeText(text string) string {
	return reWhitespace.ReplaceAllString(strings.TrimSpace(text), " ")
}

// extractMarkdownEntries turns a markdown document into a flat, deduped list of
// entries. Headings become an "A > B: " context prefix on entries beneath them;
// bullets and paragraphs each become one entry; code blocks and table rows are
// skipped. Direct port of the upstream extract_markdown_entries.
func extractMarkdownEntries(text string) []string {
	var entries []string
	var headings []string
	var paragraph []string

	contextPrefix := func() string {
		var filtered []string
		for _, h := range headings {
			if h != "" && !reMemFileNames.MatchString(h) {
				filtered = append(filtered, h)
			}
		}
		return strings.Join(filtered, " > ")
	}

	flush := func() {
		if len(paragraph) == 0 {
			return
		}
		parts := make([]string, len(paragraph))
		for i, l := range paragraph {
			parts[i] = strings.TrimSpace(l)
		}
		block := strings.TrimSpace(strings.Join(parts, " "))
		paragraph = paragraph[:0]
		if block == "" {
			return
		}
		if prefix := contextPrefix(); prefix != "" {
			entries = append(entries, prefix+": "+block)
		} else {
			entries = append(entries, block)
		}
	}

	inCode := false
	for _, raw := range strings.Split(text, "\n") {
		line := strings.TrimRight(raw, " \t\r")
		stripped := strings.TrimSpace(line)

		if strings.HasPrefix(stripped, "```") {
			inCode = !inCode
			flush()
			continue
		}
		if inCode {
			continue
		}

		if m := reHeading.FindStringSubmatch(stripped); m != nil {
			flush()
			level := len(m[1])
			value := strings.TrimSpace(m[2])
			for len(headings) >= level {
				headings = headings[:len(headings)-1]
			}
			headings = append(headings, value)
			continue
		}

		if m := reBullet.FindStringSubmatch(line); m != nil {
			flush()
			content := strings.TrimSpace(m[1])
			if prefix := contextPrefix(); prefix != "" {
				entries = append(entries, prefix+": "+content)
			} else {
				entries = append(entries, content)
			}
			continue
		}

		if stripped == "" {
			flush()
			continue
		}
		if strings.HasPrefix(stripped, "|") && strings.HasSuffix(stripped, "|") {
			flush()
			continue
		}
		paragraph = append(paragraph, stripped)
	}
	flush()

	return dedupeEntries(entries)
}

// dedupeEntries drops empty and normalize-equal duplicate entries, preserving
// first-seen order and the original (un-normalized) text.
func dedupeEntries(entries []string) []string {
	var out []string
	seen := map[string]struct{}{}
	for _, e := range entries {
		n := normalizeText(e)
		if n == "" {
			continue
		}
		if _, ok := seen[n]; ok {
			continue
		}
		seen[n] = struct{}{}
		out = append(out, strings.TrimSpace(e))
	}
	return out
}

// parseEntries reads an existing memory file into entries. See parseEntriesText.
// Returns nil for a missing or empty file.
func parseEntries(path string) []string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	return parseEntriesText(string(raw))
}

// parseEntriesText splits content on `§` or parses as markdown, round-tripping both formats.
func parseEntriesText(s string) []string {
	if strings.TrimSpace(s) == "" {
		return nil
	}
	if strings.Contains(s, entryDelimiter) {
		var out []string
		for _, part := range strings.Split(s, entryDelimiter) {
			if p := strings.TrimSpace(part); p != "" {
				out = append(out, p)
			}
		}
		return out
	}
	return extractMarkdownEntries(s)
}

// mergeStats reports what mergeEntries did, surfaced in the report details.
type mergeStats struct {
	existing   int
	added      int
	duplicates int
	overflowed int
}

// mergeEntries appends incoming entries to existing ones, skipping normalize-equal
// duplicates and stopping entries that would push the serialized length past
// limit (returned as overflowed). Char counts use code points to match the
// upstream char-limit semantics. Direct port of the upstream merge_entries.
func mergeEntries(existing, incoming []string, limit int) ([]string, mergeStats, []string) {
	merged := append([]string(nil), existing...)
	seen := map[string]struct{}{}
	for _, e := range existing {
		if strings.TrimSpace(e) != "" {
			seen[normalizeText(e)] = struct{}{}
		}
	}
	stats := mergeStats{existing: len(existing)}
	var overflowed []string

	currentLen := 0
	if len(merged) > 0 {
		currentLen = charLen(strings.Join(merged, entryDelimiter))
	}

	for _, entry := range incoming {
		n := normalizeText(entry)
		if n == "" {
			continue
		}
		if _, ok := seen[n]; ok {
			stats.duplicates++
			continue
		}
		var candidate int
		if len(merged) == 0 {
			candidate = charLen(entry)
		} else {
			candidate = currentLen + charLen(entryDelimiter) + charLen(entry)
		}
		if candidate > limit {
			stats.overflowed++
			overflowed = append(overflowed, entry)
			continue
		}
		merged = append(merged, entry)
		seen[n] = struct{}{}
		currentLen = candidate
		stats.added++
	}
	return merged, stats, overflowed
}

// entryFormat serializes merged entries for a destination file.
type entryFormat int

const (
	hermesFormat entryFormat = iota
	openclawFormat
)

func (f entryFormat) serialize(entries []string) string {
	if len(entries) == 0 {
		return ""
	}
	switch f {
	case openclawFormat:
		var sb strings.Builder
		for _, e := range entries {
			sb.WriteString("- ")
			sb.WriteString(e)
			sb.WriteString("\n")
		}
		return sb.String()
	default: // hermesFormat
		return strings.Join(entries, entryDelimiter) + "\n"
	}
}

// Brand rewriting: keeps capitalization; lowercase stays lowercase.

var reUpper = regexp.MustCompile(`[A-Z]`)

func isLower(s string) bool {
	hasLetter := false
	for _, r := range s {
		if r >= 'A' && r <= 'Z' {
			return false
		}
		if r >= 'a' && r <= 'z' {
			hasLetter = true
		}
	}
	return hasLetter
}

func casePreserving(replacement string) func(string) string {
	lower := reUpper.ReplaceAllStringFunc(replacement, func(s string) string {
		return string(s[0] - 'A' + 'a')
	})
	return func(match string) string {
		if isLower(match) {
			return lower
		}
		return replacement
	}
}
