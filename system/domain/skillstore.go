package domain

// Types for the Autonomous Agent Skills catalog (`bff-web-service`, documented
// in agent-skills-public-api.md). The device proxies the catalog's public read
// endpoints so the web UI never talks to it directly — see
// system/server/agent/delivery/http/handler_skills.go.

// SkillDraft is a user-authored skill submitted by the web UI's "Write skill"
// form. It maps onto a SKILL.md: Name + Description become the YAML
// front-matter, Instructions becomes the markdown body.
type SkillDraft struct {
	Name         string `json:"name" binding:"required"`
	Description  string `json:"description" binding:"required"`
	Instructions string `json:"instructions" binding:"required"`
}

// SkillNode is one entry in an installed skill's file tree. children is set
// only on directories; a node with no children is a file.
type SkillNode struct {
	Name string `json:"name"`
	// Path is relative to the skills root, e.g. "music/reference/tempo.md".
	Path     string      `json:"path"`
	Dir      bool        `json:"dir,omitempty"`
	Size     int64       `json:"size,omitempty"`
	Children []SkillNode `json:"children,omitempty"`
}

// InstalledSkill is one skill present in the active runtime's skills dir.
// Name is the directory name (rendered as "/music" in the UI), Description is
// read from the SKILL.md front-matter when present.
type InstalledSkill struct {
	Name        string      `json:"name"`
	Description string      `json:"description,omitempty"`
	Files       []SkillNode `json:"files"`
	// UpdatedAt is the NEWEST modification time in the skill's tree, as Unix
	// seconds — the skill directory's own mtime only moves when files are added
	// or removed, so it would call an edited SKILL.md unchanged. 0 when nothing
	// in the tree could be stat'd, which the UI renders as unknown rather than
	// as the epoch.
	UpdatedAt int64 `json:"updated_at,omitempty"`
}

// SkillSummary is one installed skill flattened for the device's status
// uplinks — the MQTT `info` response and the HTTP backend ping both carry a
// `skills` array of these.
//
// Deliberately name+description only, NOT the file tree InstalledSkill also
// holds: both uplinks are periodic (the ping every 15s), so shipping full
// per-skill trees that often would be pure waste. Consumers that want the tree
// ask for it on demand (GET /api/agent/skills/files).
//
// One type for both uplinks so their wire shape can never drift apart.
type SkillSummary struct {
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
}

// SummarizeSkills flattens a ListSkills result for the status uplinks. Returns
// nil for an empty list so the `skills` field is omitted rather than sent as [].
func SummarizeSkills(list []InstalledSkill) []SkillSummary {
	if len(list) == 0 {
		return nil
	}
	out := make([]SkillSummary, 0, len(list))
	for _, s := range list {
		out = append(out, SkillSummary{Name: s.Name, Description: s.Description})
	}
	return out
}

// StoreSkillChangelog is one released version's entry in a skill's changelog.
type StoreSkillChangelog struct {
	Version string   `json:"version"`
	Date    string   `json:"date"`
	Changes []string `json:"changes"`
}

// StoreSkill mirrors the catalog's `Skill` schema. Field names match the
// upstream snake_case JSON so the payload passes through unmapped.
type StoreSkill struct {
	ID            string                `json:"id"`
	Name          string                `json:"name"`
	Slug          string                `json:"slug"`
	Description   string                `json:"description"`
	Version       string                `json:"version"`
	CategoryID    string                `json:"category_id"`
	PlanRequired  string                `json:"plan_required"`
	Author        string                `json:"author"`
	License       string                `json:"license"`
	Size          string                `json:"size"`
	IconURL       string                `json:"icon_url"`
	FileURL       string                `json:"file_url"`
	Compatibility []string              `json:"compatibility"`
	Changelog     []StoreSkillChangelog `json:"changelog"`
	DownloadCount int64                 `json:"download_count"`
	Status        int32                 `json:"status"`
	CreatorType   string                `json:"creator_type"`
	Source        string                `json:"source"`
	EnableCount   int64                 `json:"enable_count"`
}

// StoreSkillList is the `data` payload of GET /api/v1/agent-skills.
type StoreSkillList struct {
	Data  []StoreSkill `json:"data"`
	Total int64        `json:"total"`
}

// SkillBundleFile is one file extracted from a downloaded `.skill` archive.
// Text content is inlined so the web UI can render it without a second
// round-trip; binary or oversized files carry metadata only.
type SkillBundleFile struct {
	// Path is the entry path inside the archive, e.g. "my-skill/SKILL.md".
	Path string `json:"path"`
	Size int64  `json:"size"`
	Text string `json:"text,omitempty"`
	// Binary marks a file whose bytes aren't valid UTF-8 text.
	Binary bool `json:"binary,omitempty"`
	// Truncated marks a text file cut off at the inline cap.
	Truncated bool `json:"truncated,omitempty"`
}

// SkillBundle is the unpacked contents of a skill archive.
type SkillBundle struct {
	ID    string            `json:"id"`
	Files []SkillBundleFile `json:"files"`
	// Skipped counts entries dropped by the file-count cap.
	Skipped int `json:"skipped,omitempty"`
}
