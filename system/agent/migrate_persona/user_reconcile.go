package migratepersona

import (
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/usercanon"
)

// userProfileBootstrapCap mirrors OpenClaw's agents.defaults.bootstrapMaxChars
// (12000) — the per-file ceiling on a bootstrap file injected into the system
// prompt. userProfileWarnChars is where we start warning, leaving room to react
// before anything is actually cut.
const (
	userProfileBootstrapCap = 12000
	userProfileWarnChars    = 9000
)

// ReconcileAction is one change a reconcile pass wants to make (dry-run) or has
// made (execute), for the startup log.
type ReconcileAction struct {
	Path   string // the USER.md that holds it
	Kind   string // "name" | "users-block"
	Detail string // what is being retired, e.g. `**Name:** "Leo"`
	Reason string // why, e.g. `no enrollment for "leo"`
}

func (a ReconcileAction) String() string {
	return fmt.Sprintf("%s: retire %s (%s)", a.Path, a.Detail, a.Reason)
}

// usersBlockRe matches a `## Users` block entry naming a person, e.g.
// `Users: **long (friend)**: prefers Vietnamese`. Written by the agent's daily
// profile sync (see runtimes/openclaw HEARTBEAT block) and keyed by the person's
// ENROLLMENT LABEL so the prune below can check it against the enrollment store.
//
// The `Users: ` heading prefix is OPTIONAL because it is not stable: entry
// extraction adds it from the `## Users` heading, but serializing back out
// flattens headings away, so the same block round-trips both with and without.
//
// The `(role)` parenthetical is REQUIRED, and that is the safety catch. Without
// it this would also match ordinary field bullets — `**Notes:** …` would parse
// as a person named "Notes:", resolve to no enrollment, and be deleted. The
// parenthetical is what distinguishes "a person" from "a field".
var usersBlockRe = regexp.MustCompile(`(?i)^(?:Users:\s*)?\*\*([^*(]+?)\s*\([^)]*\)\*\*`)

// ReconcileUserProfiles retires people from every runtime's USER.md who no
// longer have a face/voice enrollment on this device.
//
// WHY THIS EXISTS. USER.md is a bootstrap file — it is injected into the agent's
// system prompt on EVERY turn — but nothing on the device ever updated it. The
// agent writes what it learns to KNOWLEDGE.md and memory/*.md (which OpenClaw
// does not even load), so the file that is always read was the one that was
// never written. A device that changed hands kept greeting its previous owner by
// name for two months (lamp-ac82, 2026-09-03).
//
// THE RULE, and the only one: a name is stale when it resolves to no directory
// under usercanon.UsersDir. Absence is NEVER the trigger — a person away for a
// day, a month or a year keeps their enrollment directory and therefore their
// profile. Only an explicit /face/remove, /speaker/remove or a factory reset
// takes a directory away, and those are the acts that mean "this person is
// gone". Resolution goes through usercanon.Resolve, the same resolver the
// attribution path uses, so a display name like "Long Trần" still matches the
// `long` enrollment instead of being read as stale.
//
// COST. Reads one small file per runtime plus one directory listing, and WRITES
// ONLY WHEN SOMETHING CHANGED. That last part is load-bearing: USER.md sits in
// the ~28k-token cached prompt prefix, so an unconditional rewrite would cost a
// prompt-cache miss (~39k tokens re-billed, slower first token) on the next turn
// of every boot. Reconciling in place and writing nothing is the normal case.
//
// Every runtime is reconciled, not just the active one: persona files are copies
// and an untouched stale copy migrates back on the next runtime switch.
//
// With execute=false nothing is written — the actions are returned for logging
// so a pass can be inspected on a live device before it is allowed to bite.
func ReconcileUserProfiles(opts Options, execute bool) ([]ReconcileAction, error) {
	enrolled, err := enrolledLabels()
	if err != nil {
		// No enrollment store means we cannot tell stale from absent. Do
		// nothing rather than guess — wiping every profile because a disk is
		// not mounted yet would be far worse than a stale name.
		return nil, fmt.Errorf("read enrollment store: %w", err)
	}
	if len(enrolled) == 0 {
		// A device that has never enrolled anyone (fresh setup, mid-onboarding)
		// would otherwise have every profile retired on first boot.
		return nil, nil
	}

	var actions []ReconcileAction
	for _, path := range UserProfilePaths(opts) {
		acts, err := reconcileOneUserProfile(path, enrolled, execute)
		if err != nil {
			return actions, fmt.Errorf("reconcile %s: %w", path, err)
		}
		actions = append(actions, acts...)
	}
	return actions, nil
}

// UserProfilePaths returns every runtime's USER.md, deduped and sorted. Sourced
// from the adapters so a new runtime cannot be forgotten.
func UserProfilePaths(opts Options) []string {
	seen := map[string]bool{}
	var out []string
	for _, a := range adapters {
		p := a.userProfilePath(opts)
		if p == "" || seen[p] {
			continue
		}
		seen[p] = true
		out = append(out, p)
	}
	sort.Strings(out)
	return out
}

// enrolledLabels lists the canonical user directories that currently exist.
// "unknown" is excluded: it is the bucket for unidentified people, not a person,
// so it must never keep a profile alive.
func enrolledLabels() (map[string]bool, error) {
	ents, err := os.ReadDir(usercanon.UsersDir)
	if err != nil {
		return nil, err
	}
	out := map[string]bool{}
	for _, e := range ents {
		if e.IsDir() && e.Name() != usercanon.DefaultUser {
			out[e.Name()] = true
		}
	}
	return out, nil
}

// isEnrolled reports whether a human-readable name still has an enrollment.
func isEnrolled(name string, enrolled map[string]bool) bool {
	return enrolled[usercanon.Resolve(name)]
}

func reconcileOneUserProfile(path string, enrolled map[string]bool, execute bool) ([]ReconcileAction, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	entries := parseEntriesText(string(raw))

	var actions []ReconcileAction
	out := make([]string, 0, len(entries))
	// Blank slots already emitted, so retiring a value cannot leave a SECOND
	// empty slot behind. The live device had the stale name appended below the
	// template's own blank "- **Name:**" (earlier entry-merges did that), so
	// clearing it in place produced two blank slots — a form asking the same
	// question twice, and the shape B1 exists to prevent.
	blankSlot := map[string]bool{}
	for _, e := range entries {
		field := userFieldOf(e)
		if field != "" {
			mt := userFieldEntryRe.FindStringSubmatch(strings.TrimSpace(e))
			key := strings.ToLower(field)
			if !hasRealFieldValue(mt[2]) {
				// An empty slot. Keep the first, drop any repeat.
				if blankSlot[key] {
					continue
				}
				blankSlot[key] = true
				out = append(out, e)
				continue
			}
		}
		// A stale name is cleared back to the blank slot, never deleted: USER.md
		// is a form, and the empty "- **Name:**" line is the prompt asking for
		// the next answer. When the form already carries that blank slot above,
		// the retired bullet is simply dropped instead.
		if strings.EqualFold(field, "Name") {
			mt := userFieldEntryRe.FindStringSubmatch(strings.TrimSpace(e))
			value := strings.TrimSpace(mt[2])
			if hasRealFieldValue(value) && !isEnrolled(value, enrolled) {
				actions = append(actions, ReconcileAction{
					Path: path, Kind: "name",
					Detail: fmt.Sprintf("**Name:** %q", value),
					Reason: fmt.Sprintf("no enrollment for %q", usercanon.Resolve(value)),
				})
				if !blankSlot["name"] {
					blankSlot["name"] = true
					out = append(out, "**Name:**")
				}
				continue
			}
		}
		// A `## Users` block for someone with no enrollment is dropped whole.
		if mt := usersBlockRe.FindStringSubmatch(strings.TrimSpace(e)); mt != nil {
			label := strings.TrimSpace(mt[1])
			if !isEnrolled(label, enrolled) {
				actions = append(actions, ReconcileAction{
					Path: path, Kind: "users-block",
					Detail: fmt.Sprintf("Users block %q", label),
					Reason: fmt.Sprintf("no enrollment for %q", usercanon.Resolve(label)),
				})
				continue
			}
		}
		out = append(out, e)
	}

	// USER.md is a bootstrap file: OpenClaw injects it into the system prompt on
	// every turn, capped at bootstrapMaxChars (12000). Over that it is truncated
	// with text.slice(0, cutPoint) — i.e. the HEAD is kept and the TAIL is cut,
	// and the tail is exactly where `## Users` lives. So an oversized profile
	// loses the person data first, silently, and the only visible symptom is the
	// agent forgetting who someone is. The daily sync is told to keep entries
	// short; this is the check that the instruction was actually followed.
	if n := len(raw); n > userProfileWarnChars {
		slog.Warn("USER.md is approaching the bootstrap cap; `## Users` is at the end of the file and is truncated first",
			"component", "user-reconcile", "path", path, "chars", n,
			"warn_at", userProfileWarnChars, "cap", userProfileBootstrapCap)
	}

	if len(actions) == 0 || !execute {
		return actions, nil
	}
	// Back up before retiring anything. This pass deletes from a live persona,
	// and unlike a migration it can fire unattended on any boot — the operator
	// is not standing there to undo it. Same `.bak-<nano>` convention as
	// baseMigrator.backup, and cheap because a write only happens when
	// something was actually stale.
	if err := backupFile(path); err != nil {
		return actions, fmt.Errorf("backup before retire: %w", err)
	}
	if err := writeEntriesAtomic(path, out, openclawFormat); err != nil {
		return actions, err
	}
	return actions, nil
}

// backupFile copies path aside as "<path>.bak-<unixnano>" before it is rewritten.
func backupFile(path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return os.WriteFile(fmt.Sprintf("%s.bak-%d", path, time.Now().UnixNano()), data, 0o644)
}

// writeEntriesAtomic writes via a temp file + rename so a live agent reading
// USER.md mid-turn can never see a half-written file. Migration can get away
// with a plain write because it runs at a switch, when no turn is in flight;
// this reconcile runs while the gateway is up.
func writeEntriesAtomic(path string, entries []string, format entryFormat) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".USER.md.tmp-*")
	if err != nil {
		return fmt.Errorf("create temp: %w", err)
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }() // no-op once renamed

	if _, err := tmp.WriteString(format.serialize(entries)); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp: %w", err)
	}
	if err := os.Chmod(tmpName, 0o644); err != nil {
		return fmt.Errorf("chmod temp: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("rename: %w", err)
	}
	return nil
}
