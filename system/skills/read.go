package skills

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"

	"go.autonomous.ai/os/system/domain"
)

// Reading an installed skill's files for the Manage-skills detail view, which
// shows the same two-pane browser as the store preview. Same per-backend split
// as ListInstalled: only the directory differs.

const (
	// readMaxInlineBytes caps how much of one file is inlined as text; longer
	// files come back marked Truncated.
	readMaxInlineBytes = 512 << 10
	// readMaxFileBytes caps how much of one file is read off disk at all.
	readMaxFileBytes = 2 << 20
	readMaxFiles     = 500
	readMaxDepth     = 6
	// binarySniffSize is how many leading bytes decide text-vs-binary.
	binarySniffSize = 8000
)

// ErrSkillFileNotFound is returned when an entry path is not a readable file in
// a skill. It is separate from ErrSkillNotFound so callers can report an exact
// path mismatch without treating the whole skill as absent.
var ErrSkillFileNotFound = errors.New("skill file not found")

// ReadSkillFiles returns every file in <skillsDir>/<name> as a flat list with
// UTF-8 contents inlined, sorted by path. Deliberately flat (not a tree) so the
// detail view renders identically to the store preview, which reads a zip.
func ReadSkillFiles(skillsDir, name string) ([]domain.SkillBundleFile, error) {
	if err := ValidateSkillName(name); err != nil {
		return nil, err
	}
	if skillsDir == "" {
		return nil, fmt.Errorf("skills dir is not configured")
	}

	root := filepath.Join(skillsDir, name)
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return nil, fmt.Errorf("skill %q not found", name)
	}

	var out []domain.SkillBundleFile
	if err := collectSkillFiles(root, name, 0, &out); err != nil {
		return nil, err
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out, nil
}

// ReadSkillFilesFrom tries each root in order and returns the first skill that
// matches — for runtimes that namespace their skills dir (Hermes). Pass the
// device-owned root first, matching ListInstalledFrom's precedence so the
// detail view shows the same skill the listing did.
func ReadSkillFilesFrom(name string, dirs ...string) ([]domain.SkillBundleFile, error) {
	var lastErr error
	for _, dir := range dirs {
		files, err := ReadSkillFiles(dir, name)
		if err == nil {
			return files, nil
		}
		lastErr = err
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("skill %q not found", name)
	}
	return nil, lastErr
}

// ReadSkillFile reads one file from an installed skill. filePath must be the
// exact relative path reported by ReadSkillFiles, including the skill name.
// Keeping this separate from ReadSkillFiles avoids loading every reference and
// asset before an MQTT caller can receive one requested document.
func ReadSkillFile(skillsDir, name, filePath string) (domain.SkillBundleFile, error) {
	if err := ValidateSkillName(name); err != nil {
		return domain.SkillBundleFile{}, err
	}
	if skillsDir == "" {
		return domain.SkillBundleFile{}, fmt.Errorf("skills dir is not configured")
	}

	rel, err := skillFileRelativePath(name, filePath)
	if err != nil {
		return domain.SkillBundleFile{}, err
	}
	root := filepath.Join(skillsDir, name)
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return domain.SkillBundleFile{}, fmt.Errorf("%w: skill %q", ErrSkillNotFound, name)
	}

	full := filepath.Join(root, filepath.FromSlash(rel))
	info, err = os.Stat(full)
	if err != nil || info.IsDir() {
		return domain.SkillBundleFile{}, fmt.Errorf("%w: %s", ErrSkillFileNotFound, filePath)
	}
	content, err := readCapped(full)
	if err != nil {
		return domain.SkillBundleFile{}, fmt.Errorf("read %s: %w", filePath, err)
	}
	return BuildFilePreview(filePath, content, info.Size()), nil
}

// ReadSkillFileFrom mirrors ReadSkillFilesFrom's root precedence. A skill in
// the first root wins even if the requested entry is absent there.
func ReadSkillFileFrom(name, filePath string, dirs ...string) (domain.SkillBundleFile, error) {
	var lastErr error
	for _, dir := range dirs {
		file, err := ReadSkillFile(dir, name, filePath)
		if err == nil {
			return file, nil
		}
		if !errors.Is(err, ErrSkillNotFound) {
			return domain.SkillBundleFile{}, err
		}
		lastErr = err
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("%w: %s", ErrSkillNotFound, name)
	}
	return domain.SkillBundleFile{}, lastErr
}

func skillFileRelativePath(name, filePath string) (string, error) {
	prefix := name + "/"
	if !strings.HasPrefix(filePath, prefix) || path.Clean(filePath) != filePath {
		return "", fmt.Errorf("%w: %s", ErrSkillFileNotFound, filePath)
	}
	rel := strings.TrimPrefix(filePath, prefix)
	if rel == "" {
		return "", fmt.Errorf("%w: %s", ErrSkillFileNotFound, filePath)
	}
	parts := strings.Split(rel, "/")
	if len(parts) > readMaxDepth || strings.HasPrefix(parts[len(parts)-1], ".") {
		return "", fmt.Errorf("%w: %s", ErrSkillFileNotFound, filePath)
	}
	for _, part := range parts {
		if part == "" || strings.HasPrefix(part, ".") {
			return "", fmt.Errorf("%w: %s", ErrSkillFileNotFound, filePath)
		}
	}
	return rel, nil
}

// collectSkillFiles walks dir, appending each file with its content. relBase is
// the path prefix (relative to the skills root) so paths read "music/SKILL.md".
func collectSkillFiles(dir, relBase string, depth int, out *[]domain.SkillBundleFile) error {
	if depth >= readMaxDepth || len(*out) >= readMaxFiles {
		return nil
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("read %s: %w", dir, err)
	}

	for _, e := range entries {
		if len(*out) >= readMaxFiles {
			return nil
		}
		name := e.Name()
		if strings.HasPrefix(name, ".") {
			continue
		}
		rel := path.Join(relBase, name)

		if e.IsDir() {
			// One unreadable subdirectory must not fail the whole skill.
			_ = collectSkillFiles(filepath.Join(dir, name), rel, depth+1, out)
			continue
		}

		full := filepath.Join(dir, name)
		var size int64
		if info, err := e.Info(); err == nil {
			size = info.Size()
		}
		content, err := readCapped(full)
		if err != nil {
			continue
		}
		*out = append(*out, BuildFilePreview(rel, content, size))
	}
	return nil
}

// readCapped reads at most readMaxFileBytes from path. LimitReader rather than
// ReadFile so one oversized file can't pull the whole thing into memory.
func readCapped(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return io.ReadAll(io.LimitReader(f, readMaxFileBytes))
}

// BuildFilePreview decides how a file's bytes are presented: valid UTF-8
// without NUL bytes is inlined as text (truncated at readMaxInlineBytes),
// anything else is reported as binary with metadata only. Shared by the
// installed-skill reader and the store-bundle preview so both surfaces make the
// same call on the same bytes.
func BuildFilePreview(filePath string, content []byte, declaredSize int64) domain.SkillBundleFile {
	size := declaredSize
	if size <= 0 {
		size = int64(len(content))
	}
	file := domain.SkillBundleFile{Path: filePath, Size: size}

	sniff := content
	if len(sniff) > binarySniffSize {
		sniff = sniff[:binarySniffSize]
	}
	if !utf8.Valid(sniff) || strings.IndexByte(string(sniff), 0) >= 0 {
		file.Binary = true
		return file
	}

	if len(content) > readMaxInlineBytes {
		content = content[:readMaxInlineBytes]
		file.Truncated = true
		// Don't cut a multi-byte rune in half.
		for len(content) > 0 && !utf8.Valid(content) {
			content = content[:len(content)-1]
		}
	}
	file.Text = string(content)
	return file
}
