package hermes

import "testing"

func TestRewriteSoulName_ReplacesExisting(t *testing.T) {
	in := "# Soul\n\nYou are Lamp.\n\n## Your identity card\n\n- **Name:** Lamp\n"
	got := rewriteSoulName(in, "Ngân")
	want := "# Soul\n\nYou are Lamp.\n\n## Your identity card\n\n- **Name:** Ngân\n"
	if got != want {
		t.Fatalf("rewrite mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func TestRewriteSoulName_AppendsWhenAbsent(t *testing.T) {
	in := "# Soul\n\nYou are Lamp.\n"
	got := rewriteSoulName(in, "Ngân")
	want := "# Soul\n\nYou are Lamp.\n\n## Your identity card\n\n- **Name:** Ngân\n"
	if got != want {
		t.Fatalf("append mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func TestRewriteSoulName_PreservesBulletPrefix(t *testing.T) {
	// A non-bullet name line keeps whatever prefix precedes **Name:**.
	in := "**Name:** Old Description that should be dropped\n"
	got := rewriteSoulName(in, "Ngân")
	want := "**Name:** Ngân\n"
	if got != want {
		t.Fatalf("prefix mismatch:\n got=%q\nwant=%q", got, want)
	}
}
