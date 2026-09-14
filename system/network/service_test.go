package network

import "testing"

// TestParseDefaultRouteIface covers the route-table shapes that decide which
// interface the device reports as its own — the wired case is the one that used
// to be impossible, because the interface was hardcoded to wlan0.
func TestParseDefaultRouteIface(t *testing.T) {
	tests := []struct {
		name string
		out  string
		want string
	}{
		{
			name: "wifi only",
			out:  "default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.50 metric 303\n",
			want: "wlan0",
		},
		{
			name: "ethernet only",
			out:  "default via 192.168.1.1 dev end0 proto dhcp src 192.168.1.42 metric 202\n",
			want: "end0",
		},
		{
			// Both links up: dhcpcd gives wired the lower metric, and `ip route`
			// prints ascending by metric, so the first line is the route traffic
			// actually takes.
			name: "both up, wired wins on metric",
			out: "default via 192.168.1.1 dev end0 proto dhcp src 192.168.1.42 metric 202\n" +
				"default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.50 metric 303\n",
			want: "end0",
		},
		{
			// AP mode: hostapd is up, there is no upstream, so no default route.
			// Caller falls back to the WiFi interface, which holds 192.168.100.1.
			name: "no default route",
			out:  "",
			want: "",
		},
		{
			name: "malformed line without dev",
			out:  "default via 192.168.1.1 proto dhcp metric 202\n",
			want: "",
		},
		{
			name: "dev is last token",
			out:  "default via 192.168.1.1 dev\n",
			want: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := parseDefaultRouteIface(tt.out); got != tt.want {
				t.Fatalf("parseDefaultRouteIface(%q) = %q, want %q", tt.out, got, tt.want)
			}
		})
	}
}

// TestWifiReconnectSkipReason keeps recovery from disrupting wired devices.
func TestWifiReconnectSkipReason(t *testing.T) {
	tests := []struct {
		name         string
		ssid         string
		primaryIface string
		wantSkip     bool
	}{
		{
			name:         "wifi device with wifi default route",
			ssid:         "home-wifi",
			primaryIface: "wlan0",
			wantSkip:     false,
		},
		{
			// A dropped WiFi link leaves no default route, and PrimaryInterface
			// falls back to wlan0 — this is exactly the outage the escalation
			// was written for, so it must NOT be skipped.
			name:         "wifi device, link dropped, no default route",
			ssid:         "home-wifi",
			primaryIface: "wlan0",
			wantSkip:     false,
		},
		{
			// Provisioned over ethernet: nothing to re-associate to.
			name:         "wired setup, no credentials on file",
			ssid:         "",
			primaryIface: "end0",
			wantSkip:     true,
		},
		{
			// Cable pulled from a wired-only device: still nothing to
			// re-associate to, so WiFi recovery would not help.
			name:         "wired setup, cable pulled, fallback to wlan0",
			ssid:         "",
			primaryIface: "wlan0",
			wantSkip:     true,
		},
		{
			// Both links configured. Traffic goes out the cable, so bouncing
			// wlan0 cannot fix this outage or switching to a hotspot would not help.
			name:         "wifi configured but routing over ethernet",
			ssid:         "home-wifi",
			primaryIface: "end0",
			wantSkip:     true,
		},
		{
			name:         "whitespace-only ssid counts as no credentials",
			ssid:         "   ",
			primaryIface: "wlan0",
			wantSkip:     true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			reason := wifiReconnectSkipReason(tt.ssid, tt.primaryIface)
			if gotSkip := reason != ""; gotSkip != tt.wantSkip {
				t.Fatalf("wifiReconnectSkipReason(%q, %q) = %q (skip=%v), want skip=%v",
					tt.ssid, tt.primaryIface, reason, gotSkip, tt.wantSkip)
			}
		})
	}
}
