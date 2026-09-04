package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// userProfileFieldNames are USER.md's SINGULAR fields — the ones that describe
// exactly one value each, so a second copy is a contradiction rather than extra
// detail. Everything else in USER.md (Context notes, observations) is additive
// and keeps the normal entry-merge.
//
// Matched case-insensitively.
var userProfileFieldNames = []string{
	"Name",
	"What to call them",
	"Pronouns",
	"Timezone",
}

// userFieldEntryRe matches an entry carrying a "**Field:** value" bullet. The
// value group may be empty so an UNFILLED template slot still matches — that is
// the slot we fill in place. identityFieldRe deliberately requires a value and
// would skip them.
var userFieldEntryRe = regexp.MustCompile(`^\*\*(.+?):\*\*\s*(.*)$`)

// hasRealFieldValue reports whether a field's value is filled rather than an
// empty slot or an italic placeholder hint.
func hasRealFieldValue(v string) bool {
	v = strings.TrimSpace(v)
	return v != "" && !strings.HasPrefix(v, "_(") && !strings.HasPrefix(v, "*(")
}

func isUserProfileField(name string) bool {
	for _, f := range userProfileFieldNames {
		if strings.EqualFold(f, name) {
			return true
		}
	}
	return false
}

// userFieldOf returns the profile-field name an entry carries, or "" when the
// entry is not a bullet for one of the singular fields.
func userFieldOf(entry string) string {
	mt := userFieldEntryRe.FindStringSubmatch(strings.TrimSpace(entry))
	if mt == nil {
		return ""
	}
	name := strings.TrimSpace(mt[1])
	if !isUserProfileField(name) {
		return ""
	}
	return name
}

// partitionUserFields splits entries into the filled singular-field values and
// everything else. Field bullets are removed from the remainder — filled or
// empty — because applyUserFields owns their placement; leaving them in would
// let the entry-merge append a second bullet for a field that already exists,
// which is the duplication this whole path exists to stop.
func partitionUserFields(entries []string) ([]IdentityField, []string) {
	byName := map[string]string{}
	var rest []string
	for _, e := range entries {
		field := userFieldOf(e)
		if field == "" {
			rest = append(rest, e)
			continue
		}
		mt := userFieldEntryRe.FindStringSubmatch(strings.TrimSpace(e))
		if v := strings.TrimSpace(mt[2]); hasRealFieldValue(v) {
			// Last filled value wins within one file: on a previously-merged
			// file the appended copy is the newer of the duplicates.
			byName[strings.ToLower(field)] = v
		}
	}

	var fields []IdentityField
	for _, canonical := range userProfileFieldNames {
		if v, ok := byName[strings.ToLower(canonical)]; ok {
			fields = append(fields, IdentityField{name: canonical, value: v})
		}
	}
	return fields, rest
}

// mergeUserFields applies incoming field values over existing ones: one value
// per field, incoming wins.
//
// This is the half writeMemoryEntries could never do. Entry-merge is a
// dedupe-UNION, so "**Name:** Leo" and "**Name:** Long" are two distinct strings
// and both survive — a profile could gain a name but never retire one, and the
// stale one kept being read first. Switching runtimes then propagated the pair.
//
// An absent or unfilled incoming field NEVER blanks a filled destination: a
// source runtime that simply has no profile yet must not erase the one the
// device already learned. partitionUserFields drops unfilled values, so this
// falls out of "only what is present is applied".
func mergeUserFields(existing, incoming []IdentityField) []IdentityField {
	value := map[string]string{}
	for _, f := range existing {
		value[strings.ToLower(f.name)] = f.value
	}
	for _, f := range incoming {
		value[strings.ToLower(f.name)] = f.value
	}
	var out []IdentityField
	for _, canonical := range userProfileFieldNames {
		if v, ok := value[strings.ToLower(canonical)]; ok {
			out = append(out, IdentityField{name: canonical, value: v})
		}
	}
	return out
}

// applyUserFields writes each field into entries IN PLACE, at the position its
// bullet already occupies, and drops any later bullet for the same field. A
// field with no bullet yet is appended.
//
// USER.md is a FORM, not a log: the template ships blank slots ("- **Name:**")
// under an instruction to fill them in. So the right move is to fill the slot
// where it stands, not to prune it and write the value elsewhere — that keeps
// the template's shape, ordering and prompts completely intact while still
// leaving exactly one bullet per field. Mirrors setIdentityField, which does the
// same for IDENTITY.md.
//
// Slots for fields we have no value for are left untouched: that is the form
// still asking to be filled, and the agent reads it every turn.
func applyUserFields(entries []string, fields []IdentityField) []string {
	rendered := map[string]string{}
	for _, f := range fields {
		rendered[strings.ToLower(f.name)] = "**" + f.name + ":** " + f.value
	}

	placed := map[string]bool{}
	out := make([]string, 0, len(entries)+len(fields))
	for _, e := range entries {
		field := userFieldOf(e)
		if field == "" {
			out = append(out, e)
			continue
		}
		key := strings.ToLower(field)
		text, have := rendered[key]
		if !have {
			// No value for this field — keep the empty slot exactly as it is.
			out = append(out, e)
			continue
		}
		if placed[key] {
			// A later duplicate bullet for a field already written above: the
			// retired value ("**Name:** Leo", appended beneath the blank slot by
			// earlier entry-merges). This is the retirement.
			continue
		}
		out = append(out, text)
		placed[key] = true
	}

	// A field with no slot in this file (a runtime whose template differs) is
	// appended, in canonical order.
	for _, f := range fields {
		key := strings.ToLower(f.name)
		if !placed[key] {
			out = append(out, rendered[key])
		}
	}
	return out
}

// writeUserProfile is writeMemoryEntries specialised for USER.md: the singular
// profile fields are filled in place (see applyUserFields) while the free-form
// remainder keeps the normal dedupe-union entry merge.
//
// The template itself is carried through untouched — the "Update this as you go"
// instruction, the Context prompts, the "not building a dossier" guardrail, the
// unfilled slots, the Related link. USER.md is a bootstrap file, so all of it
// reaches the agent on every turn, and it holds the only instruction telling the
// agent to maintain this file at all. mergeEntries dedupes by normalized text,
// so it costs one copy however many migrations run.
//
// The char limit bounds the free-form remainder only. The fields are a handful
// of short lines and are the point of the file — they must never be the thing
// that overflows out.
func (b *baseMigrator) writeUserProfile(kind string, incoming []string, destination string, limit int, dstFormat entryFormat) {
	existingRaw := parseEntries(destination)
	existingFields, _ := partitionUserFields(existingRaw)
	incomingFields, incomingRest := partitionUserFields(incoming)

	fields := mergeUserFields(existingFields, incomingFields)
	withFields := applyUserFields(existingRaw, fields)
	merged, stats, overflowed := mergeEntries(withFields, incomingRest, limit)

	// A field whose value changed, or a retired duplicate that was dropped, is a
	// real edit even when no new free-form entry landed.
	structureChanged := !sameEntries(withFields, existingRaw)

	details := map[string]any{
		"existing_entries":   stats.existing,
		"added_entries":      stats.added,
		"duplicate_entries":  stats.duplicates,
		"overflowed_entries": stats.overflowed,
		"profile_fields":     len(fields),
		"char_limit":         limit,
	}

	if len(incoming) == 0 && !structureChanged {
		b.record(kind, "", destination, StatusSkipped, "no importable entries found", details)
		return
	}
	if !b.opts.Execute {
		b.record(kind, "", destination, StatusMigrated, "would merge user profile", details)
		return
	}
	if stats.added == 0 && len(overflowed) == 0 && !structureChanged {
		b.record(kind, "", destination, StatusSkipped, "no new entries to import", details)
		return
	}

	if bak, err := b.backup(destination); err != nil {
		b.record(kind, "", destination, StatusError, "backup failed: "+err.Error(), nil)
		return
	} else if bak != "" {
		details["backup"] = bak
	}
	if err := os.MkdirAll(filepath.Dir(destination), 0o755); err != nil {
		b.record(kind, "", destination, StatusError, "create dest dir: "+err.Error(), nil)
		return
	}
	if err := os.WriteFile(destination, []byte(dstFormat.serialize(merged)), 0o644); err != nil {
		b.record(kind, "", destination, StatusError, "write: "+err.Error(), nil)
		return
	}
	b.record(kind, "", destination, StatusMigrated, "", details)
}

func sameEntries(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
