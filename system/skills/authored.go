package skills

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// User-authored skills — the web UI's "Write skill" form (chat composer
// "+" → Skills → Write skill) submits a name/description/instructions triple
// that lands on disk as <skillsDir>/<name>/SKILL.md.
//
// The rendering + write live here, not in a runtime package, because only the
// TARGET DIRECTORY differs per agentic runtime — the same reason the skill
// watchers in runtimes/openclaw and runtimes/hermes are near-copies that differ
// only in their skillsDir. Each runtime's AgentGateway.SaveSkill passes its own
// dir; runtimes with no device-writable skills dir don't call this at all.

// ErrInvalidSkillName is returned when a skill name is empty or has an unsafe
// shape. The name becomes a directory under the runtime's skills dir and is how
// the agent addresses the skill, so it is restricted to the same slug shape the
// rest of the skill tooling uses.
var ErrInvalidSkillName = errors.New("invalid skill name")

// ErrSkillExists is returned when a skill directory of that name is already
// present. Authoring never silently overwrites an existing skill — an OTA- or
// store-installed skill of the same name would be destroyed.
var ErrSkillExists = errors.New("skill already exists")

// skillNamePattern allows only lowercase letters, digits, dash and underscore
// (mirrors roleNamePattern in runtimes/openclaw/role_skills.go).
var skillNamePattern = regexp.MustCompile(`^[a-z0-9_-]+$`)

// maxSkillNameLen bounds the directory name. Long enough for descriptive slugs
// like "weekly-status-report", short enough to stay well inside any filesystem
// limit once nested under the runtime's skills dir.
const maxSkillNameLen = 64

// SkillMarkdownFile is the entry-point filename every skill must carry. Matches
// what RenderSkillMarkdown writes and what every skill under skills/ ships.
const SkillMarkdownFile = "SKILL.md"

// ErrMissingSkillMD is returned when an archive holds no SKILL.md at the skill
// root — without it the agent has nothing to load, so it isn't a skill.
var ErrMissingSkillMD = errors.New("archive has no " + SkillMarkdownFile + " at the skill root")

// ErrInvalidFrontMatter is returned when a SKILL.md's YAML front-matter is
// absent or missing name/description.
var ErrInvalidFrontMatter = errors.New("invalid SKILL.md front-matter")

// ParseSkillFrontMatter reads the `name` and `description` out of a SKILL.md's
// YAML front-matter — the inverse of RenderSkillMarkdown, and the shape every
// skill under skills/ uses:
//
//	---
//	name: weekly-status-report
//	description: One line the agent reads to decide whether to load the skill.
//	---
//
// Keys BEYOND name/description are tolerated and ignored — the upstream format
// allows them (anthropics/skills' algorithmic-art carries a `license:`), so
// rejecting an unknown key would refuse a perfectly valid skill.
//
// Deliberately a line scan rather than a YAML parse: the two keys we need are
// flat top-level scalars, and pulling in a YAML dependency to read them would
// accept far more shapes than this format actually allows. Consequences worth
// knowing: only top-level keys are read (a `name:` nested under a `metadata:`
// block is correctly ignored), and a folded/multi-line scalar contributes only
// its first line.
//
// Returns ErrInvalidFrontMatter when the block is absent or either required key
// is missing, so a caller that needs the name (the bare-.md upload path) can't
// proceed on a guess. Callers that only want whatever is there — the listing,
// where the directory already supplies the name — use scanFrontMatter instead.
func ParseSkillFrontMatter(content []byte) (name, description string, err error) {
	name, description, ok := scanFrontMatter(content)
	if !ok || name == "" || description == "" {
		return "", "", ErrInvalidFrontMatter
	}
	return name, description, nil
}

// scanFrontMatter is the lenient reader behind ParseSkillFrontMatter: it returns
// whatever keys it found and whether a front-matter block was present at all,
// without ruling on completeness. Kept separate so the strict upload check and
// the best-effort listing share one scanner but not one policy.
func scanFrontMatter(content []byte) (name, description string, ok bool) {
	sc := bufio.NewScanner(bytes.NewReader(content))
	sc.Buffer(make([]byte, 0, 64*1024), 1<<20)

	opened := false
	for sc.Scan() {
		raw := strings.TrimRight(sc.Text(), "\r")
		line := strings.TrimSpace(raw)

		if line == "---" {
			if !opened {
				opened = true
				continue
			}
			break // closing delimiter — whatever we collected is it
		}
		if !opened {
			// A leading blank line is fine, but any real content before the opening
			// delimiter means there is no front-matter block.
			if line == "" {
				continue
			}
			return "", "", false
		}

		// Only TOP-LEVEL keys count. Without this, a `name:` indented under a
		// `metadata:` block would be read as the skill's name.
		if raw != line {
			continue
		}

		if rest, ok := strings.CutPrefix(line, "name:"); ok {
			name = strings.Trim(strings.TrimSpace(rest), `"'`)
			continue
		}
		if rest, ok := strings.CutPrefix(line, "description:"); ok {
			description = strings.Trim(strings.TrimSpace(rest), `"'`)
		}
	}

	return name, description, opened
}

// InstallSkillMarkdown installs a BARE SKILL.md upload: the front-matter's
// `name` decides the directory, so a file with no valid front-matter is refused
// (ErrInvalidFrontMatter) rather than landing under a guessed name.
//
// Replaces an existing skill of that name, matching InstallSkillArchive —
// installing is an explicit instruction, unlike authoring, which refuses to
// clobber.
func InstallSkillMarkdown(skillsDir string, content []byte) (string, error) {
	if skillsDir == "" {
		return "", errors.New("skills dir is not configured")
	}

	name, _, err := ParseSkillFrontMatter(content)
	if err != nil {
		return "", err
	}
	if err := ValidateSkillName(name); err != nil {
		return "", err
	}

	target := filepath.Join(skillsDir, name)
	staging := target + ".new"
	if err := os.RemoveAll(staging); err != nil {
		return "", fmt.Errorf("clean staging dir: %w", err)
	}
	if err := os.MkdirAll(staging, 0755); err != nil {
		return "", fmt.Errorf("create staging dir: %w", err)
	}
	if err := os.WriteFile(filepath.Join(staging, SkillMarkdownFile), content, 0644); err != nil {
		_ = os.RemoveAll(staging)
		return "", fmt.Errorf("write %s: %w", SkillMarkdownFile, err)
	}

	// Same atomic swap as InstallSkillArchive: the previous version is moved
	// aside and restored if the rename fails.
	backup := target + ".old"
	_ = os.RemoveAll(backup)
	if _, err := os.Stat(target); err == nil {
		if err := os.Rename(target, backup); err != nil {
			_ = os.RemoveAll(staging)
			return "", fmt.Errorf("replace existing skill %s: %w", name, err)
		}
	}
	if err := os.Rename(staging, target); err != nil {
		_ = os.Rename(backup, target)
		_ = os.RemoveAll(staging)
		return "", fmt.Errorf("install skill %s: %w", name, err)
	}
	_ = os.RemoveAll(backup)

	return target, nil
}

// SlugifySkillName coerces an arbitrary label (typically an uploaded archive's
// filename stem) into the slug shape a skill directory needs: lowercased, any run
// of unsupported characters collapsed to a single dash, ends trimmed, truncated
// to the length cap.
//
// Only used as a FALLBACK name — InstallSkillArchive prefers the archive's own
// wrapping directory. Returns "" when nothing usable survives, which the caller
// surfaces as a validation error rather than inventing a name.
func SlugifySkillName(label string) string {
	var b strings.Builder
	prevDash := false
	for _, r := range strings.ToLower(strings.TrimSpace(label)) {
		switch {
		case (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '_' || r == '-':
			b.WriteRune(r)
			prevDash = r == '-'
		default:
			// Collapse runs of junk (spaces, dots, unicode) into one dash.
			if !prevDash && b.Len() > 0 {
				b.WriteByte('-')
				prevDash = true
			}
		}
	}

	out := strings.Trim(b.String(), "-_")
	if len(out) > maxSkillNameLen {
		out = strings.Trim(out[:maxSkillNameLen], "-_")
	}
	return out
}

// ValidateSkillName reports whether name is usable as a skill directory.
func ValidateSkillName(name string) error {
	if len(name) > maxSkillNameLen {
		return fmt.Errorf("%w: longer than %d characters", ErrInvalidSkillName, maxSkillNameLen)
	}
	if !skillNamePattern.MatchString(name) {
		return fmt.Errorf("%w: %q (lowercase letters, digits, _ and - only)", ErrInvalidSkillName, name)
	}
	return nil
}

// RenderSkillMarkdown builds the SKILL.md body: YAML front-matter carrying
// name + description, then the instructions as the markdown body. Matches the
// shape of the skills shipped in skills/ — the agent reads the front-matter to
// decide whether to load the skill, and the body once it does.
//
// The description is flattened to a single line: a newline inside an unquoted
// YAML scalar would terminate the value and corrupt the front-matter block.
func RenderSkillMarkdown(name, description, instructions string) string {
	desc := strings.Join(strings.Fields(description), " ")

	var b strings.Builder
	b.WriteString("---\n")
	fmt.Fprintf(&b, "name: %s\n", name)
	fmt.Fprintf(&b, "description: %s\n", desc)
	b.WriteString("---\n\n")
	b.WriteString(strings.TrimRight(instructions, "\n"))
	b.WriteString("\n")
	return b.String()
}

// ErrSkillNotFound is returned when a skill directory isn't there to remove.
var ErrSkillNotFound = errors.New("skill not found")

// DeleteSkill removes <skillsDir>/<name> and everything under it, returning the
// path it deleted. Name is validated first, so a caller can never be tricked into
// deleting outside the skills dir.
//
// Not idempotent on purpose: a missing skill returns ErrSkillNotFound rather than
// success, so a stale UI or a double-send is visible to the caller instead of
// silently reported as a deletion that never happened.
//
// The runtime is NOT restarted; every backend with a skills dir re-reads it per
// session, the same contract the write paths rely on.
func DeleteSkill(skillsDir, name string) (string, error) {
	if err := ValidateSkillName(name); err != nil {
		return "", err
	}
	if skillsDir == "" {
		return "", errors.New("skills dir is not configured")
	}

	dir := filepath.Join(skillsDir, name)
	info, err := os.Stat(dir)
	if os.IsNotExist(err) {
		return "", fmt.Errorf("%w: %s", ErrSkillNotFound, name)
	}
	if err != nil {
		return "", fmt.Errorf("stat %s: %w", dir, err)
	}
	if !info.IsDir() {
		// Something is at that path but it isn't a skill — refuse rather than
		// delete a file the caller didn't mean to name.
		return "", fmt.Errorf("%w: %s is not a skill directory", ErrSkillNotFound, name)
	}

	if err := os.RemoveAll(dir); err != nil {
		return "", fmt.Errorf("remove %s: %w", dir, err)
	}
	return dir, nil
}

// DeleteSkillFrom removes a skill from the first root that has it, for runtimes
// that namespace their skills dir (Hermes). Roots are tried in the order given —
// pass the device-owned root first, matching ListInstalledFrom's precedence, so
// an uninstall hits the same skill the listing showed.
func DeleteSkillFrom(name string, dirs ...string) (string, error) {
	var lastErr error
	for _, dir := range dirs {
		path, err := DeleteSkill(dir, name)
		if err == nil {
			return path, nil
		}
		// A name/config problem is fatal for every root — only keep looking when
		// this particular root simply didn't have the skill.
		if !errors.Is(err, ErrSkillNotFound) {
			return "", err
		}
		lastErr = err
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("%w: %s", ErrSkillNotFound, name)
	}
	return "", lastErr
}

// WriteAuthoredSkill creates <skillsDir>/<name>/SKILL.md from an authored
// draft and returns the path it wrote. Refuses to clobber an existing skill
// directory (ErrSkillExists) so a store- or OTA-installed skill can never be
// overwritten by an authoring mistake.
//
// The runtime is NOT restarted: every backend that has a skills dir picks new
// files up per session (openclaw via skills.load.watch), which is the same
// contract InstallRoleSkills relies on.
func WriteAuthoredSkill(skillsDir, name, description, instructions string) (string, error) {
	if err := ValidateSkillName(name); err != nil {
		return "", err
	}
	if strings.TrimSpace(description) == "" {
		return "", errors.New("description is required")
	}
	if strings.TrimSpace(instructions) == "" {
		return "", errors.New("instructions are required")
	}
	if skillsDir == "" {
		return "", errors.New("skills dir is not configured")
	}

	dir := filepath.Join(skillsDir, name)
	if _, err := os.Stat(dir); err == nil {
		return "", fmt.Errorf("%w: %s", ErrSkillExists, name)
	} else if !os.IsNotExist(err) {
		return "", fmt.Errorf("stat %s: %w", dir, err)
	}

	if err := os.MkdirAll(dir, 0755); err != nil {
		return "", fmt.Errorf("create skill dir %s: %w", dir, err)
	}

	path := filepath.Join(dir, "SKILL.md")
	content := RenderSkillMarkdown(name, description, instructions)
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		// Don't leave a half-made skill dir behind for the agent to load.
		_ = os.RemoveAll(dir)
		return "", fmt.Errorf("write %s: %w", path, err)
	}
	return path, nil
}
