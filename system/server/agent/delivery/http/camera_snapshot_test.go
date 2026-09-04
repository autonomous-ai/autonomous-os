package http

import "testing"

func TestCameraSnapshotURL(t *testing.T) {
	tests := []struct {
		name     string
		toolArgs string
		result   string
		want     string
	}{
		{
			name:     "saved camera snapshot",
			toolArgs: `{"command":"curl -s 'http://127.0.0.1:5001/camera/snapshot?save=true'"}`,
			result:   `{"path":"/root/.openclaw/media/hal-snapshots/snap_1710000000000.jpg"}`,
			want:     "/api/sensing/agent-snapshot/openclaw/media-hal-snapshots/snap_1710000000000.jpg",
		},
		{
			name:     "agent workspace JPEG",
			toolArgs: `curl -s http://127.0.0.1:5001/camera/snapshot?save=true`,
			result:   `{"path":"/root/.openclaw/workspace/cam_face3.jpg"}`,
			want:     "/api/sensing/agent-snapshot/openclaw/workspace/cam_face3.jpg",
		},
		{
			name:     "non camera result is not exposed",
			toolArgs: `{"command":"curl -s http://127.0.0.1:5001/servo/play"}`,
			result:   `{"path":"/root/.openclaw/media/hal-snapshots/snap_1710000000000.jpg"}`,
			want:     "",
		},
		{
			name:     "untrusted filename is not exposed",
			toolArgs: `curl -s http://127.0.0.1:5001/camera/snapshot?save=true`,
			result:   `{"path":"/etc/passwd"}`,
			want:     "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := cameraSnapshotURL(tt.toolArgs, tt.result); got != tt.want {
				t.Fatalf("cameraSnapshotURL() = %q, want %q", got, tt.want)
			}
		})
	}
}

// Codex-shaped event pair: arguments arrive only on "start", the result only on
// "end". Without the carry-over the snapshot URL is never built (lamp-0c89,
// 2026-09-04: valid frame on disk, no image in Flow Monitor).
//
// The call order here mirrors the handler exactly — remember at "start", THEN
// resolve on that same event — because an earlier version consumed the map on
// the start event and left the end event with nothing. A test that resolved
// before remembering passed against that bug.
func TestSnapshotURLForToolCallCarriesArgsFromStart(t *testing.T) {
	h := &AgentHandler{}
	args := `/bin/bash -lc "curl -s http://127.0.0.1:5001/camera/snapshot?save=true"`
	result := `{"path": "/root/.codex/media/hal-snapshots/snap_1788498402794.jpg"}`

	// start: args present, no result yet.
	h.rememberToolArgs("item_1", args)
	if got := h.snapshotURLForToolCall("item_1", args, ""); got != "" {
		t.Fatalf("start event must not yield a URL, got %q", got)
	}

	// end: result present, args gone.
	want := "/api/sensing/agent-snapshot/codex/media-hal-snapshots/snap_1788498402794.jpg"
	if got := h.snapshotURLForToolCall("item_1", "", result); got != want {
		t.Fatalf("end event: got %q, want %q", got, want)
	}
	// Consumed: a replayed end event must not resurrect it.
	if got := h.snapshotURLForToolCall("item_1", "", result); got != "" {
		t.Fatalf("remembered args must be consumed once, got %q", got)
	}
}

// The camera skill calls os-server's /api/vision/look, not HAL's raw snapshot
// endpoint, so the detector must recognize both.
func TestSnapshotURLForToolCallAcceptsLookEndpoint(t *testing.T) {
	h := &AgentHandler{}
	args := `/bin/bash -lc "curl -sX POST http://127.0.0.1:5000/api/vision/look -d '{}'"`
	result := `{"status":1,"data":{"path":"/root/.codex/media/hal-snapshots/snap_1788500200004.jpg","description":"an office"}}`

	h.rememberToolArgs("item_2", args)
	h.snapshotURLForToolCall("item_2", args, "")

	want := "/api/sensing/agent-snapshot/codex/media-hal-snapshots/snap_1788500200004.jpg"
	if got := h.snapshotURLForToolCall("item_2", "", result); got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}
