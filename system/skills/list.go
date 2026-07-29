package skills

import (
	"bufio"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"

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
		files, err := walkSkillTree(dir, name, 0)
		if err != nil {
			// One unreadable skill must not blank the whole list — report it
			// with an empty tree and move on.
			files = nil
		}
		out = append(out, domain.InstalledSkill{
			Name:        name,
			Description: readSkillDescription(filepath.Join(dir, "SKILL.md")),
			Files:       files,
		})
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

// walkSkillTree builds the node list for dir. relBase is the path prefix used
// for each node's Path (relative to the skills root, so "music/SKILL.md").
// Directories sort before files, each group alphabetically — the order a file
// browser is expected to show.
func walkSkillTree(dir, relBase string, depth int) ([]domain.SkillNode, error) {
	if depth >= listMaxDepth {
		return nil, nil
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	if len(entries) > listMaxEntriesPerDir {
		entries = entries[:listMaxEntriesPerDir]
	}

	nodes := make([]domain.SkillNode, 0, len(entries))
	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, ".") {
			continue
		}
		rel := path.Join(relBase, name)

		if e.IsDir() {
			children, err := walkSkillTree(filepath.Join(dir, name), rel, depth+1)
			if err != nil {
				continue
			}
			nodes = append(nodes, domain.SkillNode{
				Name: name, Path: rel, Dir: true, Children: children,
			})
			continue
		}

		node := domain.SkillNode{Name: name, Path: rel}
		if info, err := e.Info(); err == nil {
			node.Size = info.Size()
		}
		nodes = append(nodes, node)
	}

	sort.Slice(nodes, func(i, j int) bool {
		if nodes[i].Dir != nodes[j].Dir {
			return nodes[i].Dir // dirs first
		}
		return nodes[i].Name < nodes[j].Name
	})
	return nodes, nil
}

// readSkillDescription pulls `description:` out of a SKILL.md's YAML
// front-matter. Deliberately a line scan rather than a YAML parse: the
// front-matter this reads is the one RenderSkillMarkdown writes (a flat
// name/description pair), and a malformed or absent header must degrade to ""
// rather than fail the listing.
func readSkillDescription(skillMD string) string {
	f, err := os.Open(skillMD)
	if err != nil {
		return ""
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1<<20)

	inFrontMatter := false
	for sc.Scan() {
		line := strings.TrimRight(sc.Text(), "\r")
		trimmed := strings.TrimSpace(line)

		if trimmed == "---" {
			if !inFrontMatter {
				inFrontMatter = true
				continue
			}
			return "" // closing delimiter reached with no description
		}
		if !inFrontMatter {
			return "" // no front-matter block at the top of the file
		}
		if rest, ok := strings.CutPrefix(trimmed, "description:"); ok {
			return strings.Trim(strings.TrimSpace(rest), `"'`)
		}
	}
	return ""
}
