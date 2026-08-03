package skills

import (
	"fmt"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// Listing what is installed in a runtime's skills dir. Same per-backend split
// as WriteAuthoredSkill / InstallSkillArchive: only the directory differs, so
// the walk lives here once and each AgentGateway.ListSkills passes its own dir.

// listMaxDepth bounds the tree walk. Real skills are SKILL.md plus a couple of
// reference/script folders; this only stops a pathological tree (or a symlink
// loop) from producing an unbounded response.
const listMaxDepth = 6

// listMaxEntriesPerDir caps how many children one directory contributes.
const listMaxEntriesPerDir = 200

// ListInstalled returns every skill directory under skillsDir with its file
// tree, sorted by name. A missing skillsDir is not an error — an un-provisioned
// runtime simply has no skills yet, so the result is an empty list.
//
// Staging/backup dirs left by InstallSkillArchive (<name>.new / <name>.old) and
// dot-directories are skipped: they are implementation detail, not skills.
func ListInstalled(skillsDir string) ([]domain.InstalledSkill, error) {
	if skillsDir == "" {
		return nil, fmt.Errorf("skills dir is not configured")
	}

	entries, err := os.ReadDir(skillsDir)
	if err != nil {
		if os.IsNotExist(err) {
			return []domain.InstalledSkill{}, nil
		}
		return nil, fmt.Errorf("read skills dir: %w", err)
	}

	out := make([]domain.InstalledSkill, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		name := e.Name()
		if strings.HasPrefix(name, ".") ||
			strings.HasSuffix(name, ".new") || strings.HasSuffix(name, ".old") {
			continue
		}

		dir := filepath.Join(skillsDir, name)
		files, newest, err := walkSkillTree(dir, name, 0)
		if err != nil {
			// One unreadable skill must not blank the whole list — report it
			// with an empty tree and move on.
			files, newest = nil, time.Time{}
		}
		// Fall back to the directory's own mtime when the tree yielded nothing
		// to stat, so an empty skill dir still reports when it appeared.
		if newest.IsZero() {
			if info, err := e.Info(); err == nil {
				newest = info.ModTime()
			}
		}

		skill := domain.InstalledSkill{
			Name:        name,
			Description: readSkillDescription(filepath.Join(dir, "SKILL.md")),
			Files:       files,
		}
		if !newest.IsZero() {
			skill.UpdatedAt = newest.Unix()
		}
		out = append(out, skill)
	}

	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

// ListInstalledFrom merges the listings of several skill roots, for runtimes
// that namespace their skills dir instead of keeping one flat tree (Hermes:
// skills/openclaw-imports + skills/authored). Roots are scanned in the order
// given and the FIRST occurrence of a name wins — pass the device-owned root
// first so a skill the user created isn't masked by an imported one of the same
// name. A root that doesn't exist contributes nothing.
func ListInstalledFrom(dirs ...string) ([]domain.InstalledSkill, error) {
	seen := make(map[string]bool)
	var out []domain.InstalledSkill

	for _, dir := range dirs {
		list, err := ListInstalled(dir)
		if err != nil {
			return nil, err
		}
		for _, s := range list {
			if seen[s.Name] {
				continue
			}
			seen[s.Name] = true
			out = append(out, s)
		}
	}

	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	if out == nil {
		out = []domain.InstalledSkill{}
	}
	return out, nil
}

// walkSkillTree builds the node list for dir, and alongside it the NEWEST
// modification time seen anywhere in that subtree (zero when nothing could be
// stat'd). relBase is the path prefix used for each node's Path (relative to the
// skills root, so "music/SKILL.md"). Directories sort before files, each group
// alphabetically — the order a file browser is expected to show.
//
// The mtime rides along with the walk rather than getting its own pass: the
// listing is on the chat modal's open path and the files have just been read.
func walkSkillTree(dir, relBase string, depth int) ([]domain.SkillNode, time.Time, error) {
	if depth >= listMaxDepth {
		return nil, time.Time{}, nil
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, time.Time{}, err
	}
	if len(entries) > listMaxEntriesPerDir {
		entries = entries[:listMaxEntriesPerDir]
	}

	var newest time.Time
	bump := func(t time.Time) {
		if t.After(newest) {
			newest = t
		}
	}

	nodes := make([]domain.SkillNode, 0, len(entries))
	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, ".") {
			continue
		}
		rel := path.Join(relBase, name)

		if e.IsDir() {
			children, childNewest, err := walkSkillTree(filepath.Join(dir, name), rel, depth+1)
			if err != nil {
				continue
			}
			bump(childNewest)
			nodes = append(nodes, domain.SkillNode{
				Name: name, Path: rel, Dir: true, Children: children,
			})
			continue
		}

		node := domain.SkillNode{Name: name, Path: rel}
		if info, err := e.Info(); err == nil {
			node.Size = info.Size()
			bump(info.ModTime())
		}
		nodes = append(nodes, node)
	}

	sort.Slice(nodes, func(i, j int) bool {
		if nodes[i].Dir != nodes[j].Dir {
			return nodes[i].Dir // dirs first
		}
		return nodes[i].Name < nodes[j].Name
	})
	return nodes, newest, nil
}

// readSkillDescription pulls `description:` out of a SKILL.md's front-matter,
// degrading to "" when the file is absent or the header malformed — a listing
// must never fail over one bad skill. Shares ParseSkillFrontMatter so the
// listing and the upload validator share one scanner (differing only in how
// strict they are about completeness).
func readSkillDescription(skillMD string) string {
	content, err := os.ReadFile(skillMD)
	if err != nil {
		return ""
	}
	// Lenient on purpose: the directory already supplies the name, so a SKILL.md
	// carrying only a description must still show it.
	_, description, _ := scanFrontMatter(content)
	return description
}
