package config

import "testing"

func TestUserProfileReconcileDefaultsToWrite(t *testing.T) {
	c := &Config{}
	if !c.UserProfileReconcileEnabled() {
		t.Fatal("unset user_profile_reconcile must mean write (fleet default since 2026-09-16)")
	}
	off := false
	c.UserProfileReconcile = &off
	if c.UserProfileReconcileEnabled() {
		t.Fatal("explicit false must mean observe only")
	}
}
