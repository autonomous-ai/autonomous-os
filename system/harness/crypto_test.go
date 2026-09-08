package harness

import (
	"os"
	"testing"
)

func TestTrustSurvivesRestart(t *testing.T) {
	dir := t.TempDir()
	first, err := NewService(dir, Callbacks{})
	if err != nil {
		t.Fatal(err)
	}
	second, err := NewService(dir, Callbacks{})
	if err != nil {
		t.Fatal(err)
	}
	if b64(first.identity.Seed()) != b64(second.identity.Seed()) {
		t.Fatal("identity changed on restart")
	}
	info, err := os.Stat(first.path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0600 {
		t.Fatal("trust is not private")
	}
}
