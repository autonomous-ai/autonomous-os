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

// The search sweep persists the frame it centred on and returns the path in its
// own response body, so a find must surface a thumbnail the same way a snapshot
// does. Before this the sweep was the one camera path the gate did not know
// about: the JPEG was written and then dropped (#342 defect I).
func TestCameraSnapshotURLAcceptsASearchResult(t *testing.T) {
	args := `{"command":"curl -sX POST http://127.0.0.1:5001/servo/search -d '{\"target\":\"keyboard\"}'"}`
	result := `{"found":true,"kind":"keyboard","image_path":"/root/.codex/media/hal-snapshots/snap_1757500000000.jpg"}`
	want := "/api/sensing/agent-snapshot/codex/media-hal-snapshots/snap_1757500000000.jpg"
	if got := cameraSnapshotURL(args, result); got != want {
		t.Fatalf("search snapshot not surfaced: got %q, want %q", got, want)
	}
}

// hal/config.py _AGENT_CONFIG_DIRS lists six runtimes and this allow-list had
// five, so every snapshot taken on opencode was written to a path the UI then
// refused to serve — silently, since a non-match is indistinguishable from
// "this tool call was not a camera call".
func TestCameraSnapshotURLCoversEveryRuntimeHALWritesTo(t *testing.T) {
	for _, runtime := range []string{
		"openclaw", "hermes", "picoclaw", "codex", "claudecode", "opencode",
	} {
		args := `curl -s http://127.0.0.1:5001/camera/snapshot?save=true`
		result := `{"path":"/root/.` + runtime + `/media/hal-snapshots/snap_42.jpg"}`
		want := "/api/sensing/agent-snapshot/" + runtime + "/media-hal-snapshots/snap_42.jpg"
		if got := cameraSnapshotURL(args, result); got != want {
			t.Errorf("%s: got %q, want %q", runtime, got, want)
		}
	}
}

// Tool output is untrusted agent text. Naming the search endpoint must not turn
// an arbitrary path into a servable URL.
func TestCameraSnapshotURLStillRejectsAnUnapprovedSearchPath(t *testing.T) {
	args := `{"command":"curl -sX POST http://127.0.0.1:5001/servo/search -d '{}'"}`
	for _, result := range []string{
		`{"image_path":"/etc/shadow.jpg"}`,
		`{"image_path":"/root/.codex/../../etc/secret.jpg"}`,
		`{"image_path":"/root/.evilruntime/media/hal-snapshots/snap_1.jpg"}`,
	} {
		if got := cameraSnapshotURL(args, result); got != "" {
			t.Errorf("unapproved path surfaced for %s: %q", result, got)
		}
	}
}

// A sweep that found nothing writes no frame. The body still mentions the
// endpoint, so the gate must not invent a URL out of the miss.
func TestCameraSnapshotURLIgnoresASearchThatFoundNothing(t *testing.T) {
	args := `{"command":"curl -sX POST http://127.0.0.1:5001/servo/search -d '{}'"}`
	result := `{"found":false,"image_path":null,"message":"no keyboard found after 18 look(s)"}`
	if got := cameraSnapshotURL(args, result); got != "" {
		t.Fatalf("a miss produced a thumbnail: %q", got)
	}
}

// A sweep that outlives the exec tool's foreground window is backgrounded, and
// its result arrives on a later `poll` call whose args name a session, not the
// endpoint. Device-observed 2026-09-14 (lamp-ac82, three "find my doll" turns):
// the frame was written, the URL was buildable, and the card stayed blank
// because the endpoint gate looked at the poll's args. `image_path` is our own
// field name — nothing but the sweep writes it — so a result carrying it under
// an approved runtime path is trusted on the path alone.
func TestCameraSnapshotURLAcceptsASearchResultDeliveredByAPoll(t *testing.T) {
	args := `{"action":"poll","sessionId":"brisk-bison","timeout":15000}`
	result := `{"status":"ok","found":true,"kind":"doll","image_path":"/root/.openclaw/media/hal-snapshots/snap_1789363665350.jpg","looks_visited":2}`
	want := "/api/sensing/agent-snapshot/openclaw/media-hal-snapshots/snap_1789363665350.jpg"
	if got := cameraSnapshotURL(args, result); got != want {
		t.Errorf("polled search result not surfaced: got %q, want %q", got, want)
	}
}

// The relaxation is scoped to OUR key. A generic `path` in a non-camera tool's
// output must still need the endpoint in the args, and an unapproved directory
// is refused regardless of key.
func TestCameraSnapshotURLPollRelaxationIsScopedToImagePath(t *testing.T) {
	poll := `{"action":"poll","sessionId":"x"}`
	if got := cameraSnapshotURL(poll, `{"path":"/root/.openclaw/media/hal-snapshots/snap_1.jpg"}`); got != "" {
		t.Errorf("a generic path on a poll was surfaced: %q", got)
	}
	if got := cameraSnapshotURL(poll, `{"image_path":"/etc/shadow.jpg"}`); got != "" {
		t.Errorf("an unapproved image_path was surfaced: %q", got)
	}
}
