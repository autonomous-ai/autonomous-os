package http

import (
	"archive/zip"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/skills"
)

// Device-side wrapper around the Autonomous Agent Skills catalog (public read
// API, see agent-skills-public-api.md). The web UI's chat composer
// ("+" → Skills → Browse skills) goes through here rather than calling the
// catalog directly — same rationale as GET /api/plugin/browse: no CORS
// round-trip, and the catalog host stays a server-side concern.

const (
	// skillStoreTimeout bounds a listing fetch, skillDownloadTimeout an archive
	// download (bigger body, so a longer budget).
	skillStoreTimeout    = 10 * time.Second
	skillDownloadTimeout = 30 * time.Second
)

// Bundle extraction caps. Skills are small (a SKILL.md plus a handful of
// reference files), so these are generous ceilings that exist to keep a hostile
// or broken archive from exhausting device memory/disk, not to constrain real
// content.
const (
	maxBundleBytes = 16 << 20 // downloaded archive
	maxFileBytes   = 2 << 20  // one extracted file
	maxBundleFiles = 500
)

// storeEnvelope is the catalog's JSON wrapper. Business failures come back with
// HTTP 200 and a non-1 status, so every caller must check Status — not just the
// HTTP code.
type storeEnvelope struct {
	Status  int             `json:"status"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data"`
}

// storeGet delegates to the shared catalog client in system/skills — the MQTT
// downlink needs the same transport, and one copy means the two paths can't
// drift on host, header or limits.
func storeGet(path string, query url.Values, timeout time.Duration, maxBytes int64) ([]byte, error) {
	return skills.StoreGet(path, query, timeout, maxBytes)
}

// ListSkills handles GET /api/agent/skills. Returns the skills present in the
// ACTIVE runtime's skills dir, each with its file tree — the Manage-skills UI.
// A runtime with no device-readable skills dir answers 501.
func (h *AgentHandler) ListSkills(c *gin.Context) {
	list, err := h.agentGateway.ListSkills()
	if errors.Is(err, domain.ErrNotSupportedByRuntime) {
		c.JSON(http.StatusNotImplemented, serializers.ResponseError(
			"the active agent runtime ("+h.agentGateway.Name()+") cannot list skills yet"))
		return
	}
	if err != nil {
		slog.Error("[skills] list failed", "component", "agent-http", "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	if list == nil {
		list = []domain.InstalledSkill{}
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(list))
}

// ReadSkillFiles handles GET /api/agent/skills/files?name=<skill>. Returns one
// installed skill's files with text inlined — the Manage-skills detail view,
// deliberately the same `domain.SkillBundle` shape the store preview returns so
// both render through one component.
func (h *AgentHandler) ReadSkillFiles(c *gin.Context) {
	name := strings.TrimSpace(c.Query("name"))
	if name == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("name is required"))
		return
	}

	files, err := h.agentGateway.ReadSkillFiles(name)
	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		c.JSON(http.StatusNotImplemented, serializers.ResponseError(
			"the active agent runtime ("+h.agentGateway.Name()+") cannot read skills yet"))
		return
	case errors.Is(err, skills.ErrInvalidSkillName):
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	case err != nil:
		// A missing skill is the common case here (stale listing), so this is a
		// 404 rather than a 500.
		c.JSON(http.StatusNotFound, serializers.ResponseError(err.Error()))
		return
	}

	if files == nil {
		files = []domain.SkillBundleFile{}
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(domain.SkillBundle{ID: name, Files: files}))
}

// UploadSkill handles POST /api/agent/skills/upload — a skill supplied from the
// operator's machine, installed into the ACTIVE runtime's skills dir. Same
// destination and same replace semantics as installing from the catalog; only the
// source of the bytes differs.
//
// Accepted inputs mirror the upstream skill format (anthropics/skills):
//
//	.zip / .skill  an archive that MUST contain SKILL.md at the skill root
//	.md            a bare SKILL.md whose YAML front-matter MUST carry name +
//	               description — that name is what the skill is installed as
//
// Both requirements are enforced device-side, so a malformed upload is rejected
// with a 400 instead of installing something the agent can never load.
//
// Multipart (field `file`) rather than the base64-in-JSON this repo uses for face
// enrollment: that carries a small JPEG, whereas a skill archive can run to
// megabytes, and base64 would inflate it by a third for no gain.
func (h *AgentHandler) UploadSkill(c *gin.Context) {
	header, err := c.FormFile("file")
	if err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("file is required (multipart field \"file\")"))
		return
	}
	if header.Size == 0 {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("the uploaded file is empty"))
		return
	}
	if header.Size > skills.StoreMaxBytes {
		c.JSON(http.StatusRequestEntityTooLarge, serializers.ResponseError(
			fmt.Sprintf("archive is %d bytes, max %d", header.Size, int64(skills.StoreMaxBytes))))
		return
	}

	base := filepath.Base(header.Filename)
	ext := strings.ToLower(filepath.Ext(base))

	var dir string
	switch ext {
	case ".md":
		dir, err = h.installUploadedMarkdown(header)
	case ".zip", ".skill":
		dir, err = h.installUploadedArchive(header, base)
	default:
		c.JSON(http.StatusBadRequest, serializers.ResponseError(
			"unsupported file type "+ext+" — upload a .skill, .zip or .md"))
		return
	}

	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		c.JSON(http.StatusNotImplemented, serializers.ResponseError(
			"the active agent runtime ("+h.agentGateway.Name()+") cannot install skills yet"))
		return
	case errors.Is(err, skills.ErrEmptyArchive),
		errors.Is(err, skills.ErrMissingSkillMD),
		errors.Is(err, skills.ErrInvalidFrontMatter),
		errors.Is(err, skills.ErrInvalidSkillName):
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	case err != nil:
		slog.Error("[skills] upload install failed", "component", "agent-http",
			"file", header.Filename, "error", err)
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	name := filepath.Base(dir)
	slog.Info("[skills] uploaded", "component", "agent-http",
		"file", header.Filename, "skill", name, "runtime", h.agentGateway.Name(), "path", dir)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"name": name, "path": dir}))
}

// installUploadedMarkdown handles a bare SKILL.md upload: the file's own YAML
// front-matter names the skill, so nothing has to be inferred from the filename.
func (h *AgentHandler) installUploadedMarkdown(header *multipart.FileHeader) (string, error) {
	f, err := header.Open()
	if err != nil {
		return "", fmt.Errorf("open upload: %w", err)
	}
	defer f.Close()

	content, err := io.ReadAll(io.LimitReader(f, skills.StoreMaxBytes))
	if err != nil {
		return "", fmt.Errorf("read upload: %w", err)
	}
	return h.agentGateway.InstallSkillMarkdown(content)
}

// installUploadedArchive stages a `.skill`/`.zip` upload in a temp dir and hands
// it to the runtime. The fallback name is only consulted for a flat archive; a
// normal bundle's wrapping directory names the skill.
func (h *AgentHandler) installUploadedArchive(header *multipart.FileHeader, base string) (string, error) {
	tmpDir, err := os.MkdirTemp("", "skill-upload-*")
	if err != nil {
		return "", fmt.Errorf("create temp dir: %w", err)
	}
	defer os.RemoveAll(tmpDir)

	zipPath := filepath.Join(tmpDir, "skill.zip")
	if err := saveMultipartFile(header, zipPath); err != nil {
		return "", err
	}

	fallback := skills.SlugifySkillName(strings.TrimSuffix(base, filepath.Ext(base)))
	return h.agentGateway.InstallSkillArchive(zipPath, fallback)
}

// saveMultipartFile writes an uploaded part to dst. Hand-rolled rather than
// gin's SaveUploadedFile so the copy is byte-capped.
func saveMultipartFile(header *multipart.FileHeader, dst string) error {
	src, err := header.Open()
	if err != nil {
		return fmt.Errorf("open upload: %w", err)
	}
	defer src.Close()

	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0600)
	if err != nil {
		return fmt.Errorf("create temp file: %w", err)
	}
	if _, err := io.Copy(out, io.LimitReader(src, skills.StoreMaxBytes)); err != nil {
		out.Close()
		return fmt.Errorf("store upload: %w", err)
	}
	return out.Close()
}

// DeleteSkill handles DELETE /api/agent/skills?name=<skill>. Removes the skill
// from the ACTIVE runtime's skills dir via the AgentGateway. A skill that isn't
// installed is a 404, not a silent success — the caller's list was stale.
func (h *AgentHandler) DeleteSkill(c *gin.Context) {
	name := strings.TrimSpace(c.Query("name"))
	if name == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("name is required"))
		return
	}

	path, err := h.agentGateway.DeleteSkill(name)
	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		c.JSON(http.StatusNotImplemented, serializers.ResponseError(
			"the active agent runtime ("+h.agentGateway.Name()+") cannot uninstall skills yet"))
		return
	case errors.Is(err, skills.ErrSkillNotFound):
		c.JSON(http.StatusNotFound, serializers.ResponseError(err.Error()))
		return
	case errors.Is(err, skills.ErrInvalidSkillName):
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	case err != nil:
		slog.Error("[skills] uninstall failed", "component", "agent-http", "skill", name, "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	slog.Info("[skills] uninstalled", "component", "agent-http",
		"skill", name, "runtime", h.agentGateway.Name(), "path", path)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"name": name, "path": path}))
}

// SaveSkill handles POST /api/agent/skills. Writes a user-authored skill (the
// web UI's "Write skill" form) into the ACTIVE runtime's skills dir via the
// AgentGateway — each backend owns its own directory, so the device layer never
// hardcodes one.
//
// A backend that hasn't implemented it returns ErrNotSupportedByRuntime, which
// surfaces as 501: nothing was stored, and the UI says so rather than pretending
// the skill was saved.
func (h *AgentHandler) SaveSkill(c *gin.Context) {
	var draft domain.SkillDraft
	if err := c.ShouldBindJSON(&draft); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	draft.Name = strings.TrimSpace(draft.Name)
	draft.Description = strings.TrimSpace(draft.Description)
	draft.Instructions = strings.TrimSpace(draft.Instructions)

	path, err := h.agentGateway.SaveSkill(draft)
	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		c.JSON(http.StatusNotImplemented, serializers.ResponseError(
			"the active agent runtime ("+h.agentGateway.Name()+") cannot store authored skills yet"))
		return
	case errors.Is(err, skills.ErrInvalidSkillName), errors.Is(err, skills.ErrSkillExists):
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	case err != nil:
		slog.Error("[skills] save failed", "component", "agent-http", "skill", draft.Name, "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	slog.Info("[skills] saved", "component", "agent-http",
		"skill", draft.Name, "runtime", h.agentGateway.Name(), "path", path)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{
		"name": draft.Name,
		"path": path,
	}))
}

// BrowseSkills handles GET /api/agent/skills/browse. Thin pass-through of the
// catalog's GET /api/v1/agent-skills listing, forwarding the optional filters
// the web UI exposes (keyword / category_id / plan / page / limit).
func (h *AgentHandler) BrowseSkills(c *gin.Context) {
	q := url.Values{}
	// `status` is deliberately not forwarded: the catalog can't distinguish
	// "unset" from 0, so sending it would silently filter the listing.
	for _, k := range []string{"keyword", "category_id", "plan", "page", "limit"} {
		if v := strings.TrimSpace(c.Query(k)); v != "" {
			q.Set(k, v)
		}
	}

	body, err := storeGet("/api/v1/agent-skills", q, skillStoreTimeout, maxBundleBytes)
	if err != nil {
		slog.Error("[skills] browse failed", "component", "agent-http", "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError("failed to reach the skill store"))
		return
	}

	var env storeEnvelope
	if err := json.Unmarshal(body, &env); err != nil {
		slog.Error("[skills] browse: invalid envelope", "component", "agent-http", "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError("invalid JSON from the skill store"))
		return
	}
	if env.Status != 1 {
		msg := env.Message
		if msg == "" {
			msg = fmt.Sprintf("skill store returned status %d", env.Status)
		}
		c.JSON(http.StatusBadGateway, serializers.ResponseError(msg))
		return
	}

	var list domain.StoreSkillList
	if err := json.Unmarshal(env.Data, &list); err != nil {
		slog.Error("[skills] browse: invalid payload", "component", "agent-http", "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError("unexpected payload from the skill store"))
		return
	}
	if list.Data == nil {
		list.Data = []domain.StoreSkill{}
	}

	c.JSON(http.StatusOK, serializers.ResponseSuccess(list))
}

// SkillBundle handles GET /api/agent/skills/bundle?id=<skillID>. Downloads the
// skill's `.skill` archive to a temp dir, unzips it there, and returns the file
// list with text contents inlined so the web UI can render a file browser
// without a round-trip per file. The temp dir is removed before returning —
// this is a preview, not an install.
//
// The id rides a query param rather than a path segment so the route never
// collides with the sibling static `skills/browse` route.
func (h *AgentHandler) SkillBundle(c *gin.Context) {
	id := strings.TrimSpace(c.Query("id"))
	if id == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("id is required"))
		return
	}
	// The id goes into the upstream path — reject anything that could escape it.
	if strings.ContainsAny(id, "/\\?#") {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid skill id"))
		return
	}

	tmpDir, err := os.MkdirTemp("", "skill-bundle-*")
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("cannot create temp dir"))
		return
	}
	defer os.RemoveAll(tmpDir)

	// 1. Download the archive. `/download` returns a raw zip, NOT the JSON
	//    envelope — so a JSON body here means the catalog failed.
	archive, err := storeGet("/api/v1/agent-skills/"+url.PathEscape(id)+"/download",
		nil, skillDownloadTimeout, maxBundleBytes)
	if err != nil {
		slog.Error("[skills] bundle download failed", "component", "agent-http", "id", id, "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError("failed to download the skill"))
		return
	}

	zipPath := filepath.Join(tmpDir, "skill.zip")
	if err := os.WriteFile(zipPath, archive, 0600); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("cannot write temp file"))
		return
	}

	// 2. Unzip into the temp dir and read what came out.
	bundle, err := extractSkillBundle(zipPath, filepath.Join(tmpDir, "unpacked"))
	if err != nil {
		slog.Error("[skills] bundle extract failed", "component", "agent-http", "id", id, "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
		return
	}
	bundle.ID = id

	slog.Info("[skills] bundle ready", "component", "agent-http", "id", id, "files", len(bundle.Files))
	c.JSON(http.StatusOK, serializers.ResponseSuccess(bundle))
}

// InstallSkill handles POST /api/agent/skills/install with body {id, name}.
// Downloads the catalog's `.skill` archive to a temp dir and hands it to the
// ACTIVE runtime, which extracts it into its own skills dir. Same per-backend
// split as SaveSkill — a runtime that hasn't implemented it answers 501 and
// nothing is installed.
func (h *AgentHandler) InstallSkill(c *gin.Context) {
	var req struct {
		ID   string `json:"id" binding:"required"`
		Name string `json:"name"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	req.ID = strings.TrimSpace(req.ID)
	if strings.ContainsAny(req.ID, "/\\?#") {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid skill id"))
		return
	}

	tmpDir, err := os.MkdirTemp("", "skill-install-*")
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("cannot create temp dir"))
		return
	}
	defer os.RemoveAll(tmpDir)

	archive, err := storeGet("/api/v1/agent-skills/"+url.PathEscape(req.ID)+"/download",
		nil, skillDownloadTimeout, maxBundleBytes)
	if err != nil {
		slog.Error("[skills] install download failed", "component", "agent-http", "id", req.ID, "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError("failed to download the skill"))
		return
	}
	zipPath := filepath.Join(tmpDir, "skill.zip")
	if err := os.WriteFile(zipPath, archive, 0600); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("cannot write temp file"))
		return
	}

	dir, err := h.agentGateway.InstallSkillArchive(zipPath, strings.TrimSpace(req.Name))
	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		c.JSON(http.StatusNotImplemented, serializers.ResponseError(
			"the active agent runtime ("+h.agentGateway.Name()+") cannot install skills yet"))
		return
	case errors.Is(err, skills.ErrInvalidSkillName), errors.Is(err, skills.ErrEmptyArchive):
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	case err != nil:
		slog.Error("[skills] install failed", "component", "agent-http", "id", req.ID, "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	slog.Info("[skills] installed", "component", "agent-http",
		"id", req.ID, "runtime", h.agentGateway.Name(), "dir", dir)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{
		"name": filepath.Base(dir),
		"path": dir,
	}))
}

// extractSkillBundle unzips zipPath into destDir and returns the extracted
// files with text contents inlined. Path-traversal guarded; per-file and
// file-count caps applied.
func extractSkillBundle(zipPath, destDir string) (domain.SkillBundle, error) {
	var bundle domain.SkillBundle

	r, err := zip.OpenReader(zipPath)
	if err != nil {
		return bundle, fmt.Errorf("the downloaded skill is not a valid archive")
	}
	defer r.Close()

	if err := os.MkdirAll(destDir, 0700); err != nil {
		return bundle, fmt.Errorf("cannot create unpack dir")
	}
	cleanDest, err := filepath.Abs(destDir)
	if err != nil {
		return bundle, fmt.Errorf("cannot resolve unpack dir")
	}
	cleanDest = filepath.Clean(cleanDest) + string(os.PathSeparator)

	for _, f := range r.File {
		if f.FileInfo().IsDir() {
			continue
		}
		name := filepath.ToSlash(f.Name)
		if strings.HasPrefix(name, "/") || strings.Contains(name, "..") {
			return bundle, fmt.Errorf("archive contains an unsafe path")
		}
		// Editor/OS cruft that would only clutter the file list.
		if base := filepath.Base(name); base == ".DS_Store" || strings.HasPrefix(name, "__MACOSX/") {
			continue
		}
		if len(bundle.Files) >= maxBundleFiles {
			bundle.Skipped++
			continue
		}

		target := filepath.Join(destDir, filepath.FromSlash(name))
		absTarget, err := filepath.Abs(target)
		if err != nil || !strings.HasPrefix(absTarget+string(os.PathSeparator), cleanDest) {
			return bundle, fmt.Errorf("archive contains an unsafe path")
		}
		if err := os.MkdirAll(filepath.Dir(target), 0700); err != nil {
			return bundle, fmt.Errorf("cannot unpack the skill")
		}

		content, err := readZipEntry(f, target)
		if err != nil {
			return bundle, err
		}
		bundle.Files = append(bundle.Files, skills.BuildFilePreview(name, content, int64(f.UncompressedSize64)))
	}

	if len(bundle.Files) == 0 {
		return bundle, fmt.Errorf("the skill archive is empty")
	}
	return bundle, nil
}

// readZipEntry writes one entry to disk (the "unzip into temp" step) and
// returns the bytes it wrote, capped at maxFileBytes.
func readZipEntry(f *zip.File, target string) ([]byte, error) {
	rc, err := f.Open()
	if err != nil {
		return nil, fmt.Errorf("cannot read %s from the archive", f.Name)
	}
	defer rc.Close()

	content, err := io.ReadAll(io.LimitReader(rc, maxFileBytes))
	if err != nil {
		return nil, fmt.Errorf("cannot read %s from the archive", f.Name)
	}
	if err := os.WriteFile(target, content, 0600); err != nil {
		return nil, fmt.Errorf("cannot unpack the skill")
	}
	return content, nil
}
