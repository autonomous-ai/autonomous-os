// Package usercanon resolves raw user labels (AI-supplied names, Telegram
// sender strings, face-recognition ids) to the canonical user directory
// under /root/local/users.
//
// Resolve mirrors the Python hal.service.voice.music_service.canonicalize_person
// behaviour so Go-written and Python-written paths converge on the same
// folder for a given person.
package usercanon

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

const (
	DefaultUser      = "unknown"
	MaxNormalizedLen = 64
)

// UsersDir is the root of per-user directories. Overridable for tests.
var UsersDir = "/root/local/users"

var (
	reNonLabel   = regexp.MustCompile(`[^a-z0-9_-]+`)
	reAlphanum   = regexp.MustCompile(`[a-z0-9]+`)
	reTelegramID = regexp.MustCompile(`\((\d+)\)`)
)

// Slugify lowercases, collapses non [a-z0-9_-] runs to "_", trims, caps at 64.
// Matches Python FaceRecognizer.normalize_label.
func Slugify(name string) string {
	s := strings.ToLower(strings.TrimSpace(name))
	s = reNonLabel.ReplaceAllString(s, "_")
	s = strings.Trim(s, "_")
	if len(s) > MaxNormalizedLen {
		s = s[:MaxNormalizedLen]
	}
	if s == "" {
		return DefaultUser
	}
	return s
}

// Resolve maps a raw label to a canonical user dir name by trying, in order:
//  1. Slug match against an existing user dir.
//  2. Telegram id in `NAME (123456)` form → scan metadata.json for matching telegram_id.
//  3. Longest alphanumeric token that matches an existing user dir
//     (e.g. "i am gray" → "gray").
//  4. Slug fallback (may create a new dir, but stays filesystem-safe).
func Resolve(label string) string {
	if strings.TrimSpace(label) == "" {
		return DefaultUser
	}
	slug := Slugify(label)

	entries, err := os.ReadDir(UsersDir)
	if err != nil {
		return slug
	}
	existing := make(map[string]bool, len(entries))
	for _, e := range entries {
		if e.IsDir() {
			existing[e.Name()] = true
		}
	}

	if existing[slug] {
		return slug
	}

	if m := reTelegramID.FindStringSubmatch(label); len(m) == 2 {
		if name := lookupByTelegramID(m[1], existing); name != "" {
			return name
		}
	}

	tokens := reAlphanum.FindAllString(strings.ToLower(label), -1)
	sort.SliceStable(tokens, func(i, j int) bool { return len(tokens[i]) > len(tokens[j]) })
	for _, tok := range tokens {
		if existing[tok] {
			return tok
		}
	}

	return slug
}

func lookupByTelegramID(tid string, existing map[string]bool) string {
	for name := range existing {
		data, err := os.ReadFile(filepath.Join(UsersDir, name, "metadata.json"))
		if err != nil {
			continue
		}
		var meta struct {
			TelegramID any `json:"telegram_id"`
		}
		if json.Unmarshal(data, &meta) != nil {
			continue
		}
		var metaID string
		switch v := meta.TelegramID.(type) {
		case string:
			metaID = v
		case float64:
			metaID = strconv.FormatInt(int64(v), 10)
		case json.Number:
			metaID = v.String()
		}
		if metaID == tid {
			return name
		}
	}
	return ""
}
