package migratepersona

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBuildIdentityBlock_FilledFieldsOnly(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "IDENTITY.md")
	content := "# IDENTITY.md - Who Am I?\n\n_Fill this in._\n\n" +
		"- **Name:** Ngân\n" +
		"- **Creature:**\n  _(AI? robot?)_\n" +
		"- **Vibe:**\n  _(warm? calm?)_\n"
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	got := buildIdentityBlock(p)
	if !strings.Contains(got, identityCardHeading) {
		t.Fatalf("missing heading: %q", got)
	}
	if !strings.Contains(got, "- **Name:** Ngân") {
		t.Fatalf("missing name: %q", got)
	}
	if strings.Contains(got, "Creature") || strings.Contains(got, "Vibe") || strings.Contains(got, "_(") {
		t.Fatalf("unfilled placeholder leaked: %q", got)
	}
}

func TestBuildIdentityBlock_MissingFile(t *testing.T) {
	if got := buildIdentityBlock(filepath.Join(t.TempDir(), "nope.md")); got != "" {
		t.Fatalf("expected empty for missing file, got %q", got)
	}
}

func TestBuildIdentityBlock_NoFilledFields(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "IDENTITY.md")
	if err := os.WriteFile(p, []byte("- **Name:**\n  _(pick one)_\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := buildIdentityBlock(p); got != "" {
		t.Fatalf("expected empty when no filled fields, got %q", got)
	}
}
