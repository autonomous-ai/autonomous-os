package bootstrap

import "testing"

func TestCompareVersions(t *testing.T) {
	cases := []struct {
		a, b string
		want int
	}{
		{"1.2.3", "1.2.3", 0},
		{"1.2.3", "1.2.4", -1},
		{"1.3.0", "1.2.9", 1},
		{"2.0.0", "1.9.9", 1},
		// numeric, not lexical: 6 > 5
		{"2026.6.9", "2026.5.9", 1},
		{"2026.5.9", "2026.6.9", -1},
		// pre-release/build suffix ignored (numeric core only)
		{"1.2.3-rc1", "1.2.3", 0},
		{"1.2.3+build5", "1.2.3", 0},
		// "v" prefix / surrounding text tolerated via semverRe extraction
		{"v1.4.0", "1.4.0", 0},
		// empty / unparseable sorts lowest
		{"", "0.0.1", -1},
		{"", "", 0},
		{"garbage", "1.0.0", -1},
	}
	for _, c := range cases {
		if got := compareVersions(c.a, c.b); got != c.want {
			t.Errorf("compareVersions(%q, %q) = %d, want %d", c.a, c.b, got, c.want)
		}
	}
}
