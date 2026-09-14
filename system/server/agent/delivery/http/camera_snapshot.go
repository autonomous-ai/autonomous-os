package http

import (
	"regexp"
	"strings"
)

// cameraSnapshotPathRE accepts JPEGs only from the active agent runtime's
// approved camera-output directories. The UI receives a server URL, never the
// runtime's filesystem path.
//
// Keep the runtime list in step with hal/config.py `_AGENT_CONFIG_DIRS`, which
// is what decides where HAL actually writes. opencode was missing here while
// HAL was already writing into it, so every snapshot on that runtime was saved
// and then dropped — silently, because a non-match is indistinguishable from
// "this tool call was not a camera call".
var cameraSnapshotPathRE = regexp.MustCompile(`/root/\.(openclaw|hermes|picoclaw|codex|claudecode|opencode)/(workspace|media/hal-snapshots)/([A-Za-z0-9][A-Za-z0-9._-]*\.(jpg|jpeg))\b`)

// cameraSnapshotEndpoints are the calls that can leave the agent holding a
// frame. Every one of them has to be listed: the gate is an allow-list, and an
// endpoint missing from it drops the thumbnail for every turn that uses it
// without reporting anything.
var cameraSnapshotEndpoints = []string{
	// HAL's raw snapshot endpoint.
	"/camera/snapshot",
	// os-server's snapshot + describe, which the camera skill calls instead.
	"/api/vision/look",
	// The search sweep: it persists the frame it centred on and returns the
	// path in its own body, so a find carries an image the same way.
	"/servo/search",
}

// cameraSnapshotURL returns the UI-safe URL for a snapshot produced by a
// camera tool call. Tool output is untrusted agent text, so both the camera
// command and an approved runtime path must match before exposing anything.
func cameraSnapshotURL(toolArgs, result string) string {
	called := false
	for _, endpoint := range cameraSnapshotEndpoints {
		if strings.Contains(toolArgs, endpoint) {
			called = true
			break
		}
	}
	// A sweep that outlives the exec tool's foreground window is backgrounded
	// and its result arrives on a later `poll` call, whose args name a session
	// rather than the endpoint. `image_path` is the search's own field — nothing
	// else on the device writes it — so a result carrying that key is trusted on
	// the path allow-list alone. A generic `path` still needs the endpoint in
	// the args: that key appears in plenty of tool output that is not a frame.
	if !called && !strings.Contains(result, `"image_path"`) {
		return ""
	}
	matches := cameraSnapshotPathRE.FindStringSubmatch(result)
	if len(matches) != 5 {
		return ""
	}
	source := matches[2]
	if source == "media/hal-snapshots" {
		source = "media-hal-snapshots"
	}
	return "/api/sensing/agent-snapshot/" + matches[1] + "/" + source + "/" + matches[3]
}

// maxPendingToolArgs bounds toolArgsByCall so a runtime that emits "start"
// without a matching "end" (a killed turn, a dropped WS frame) cannot grow the
// map without limit. Well past the number of tools in flight in one turn.
const maxPendingToolArgs = 64

// rememberToolArgs stores a tool call's arguments at its "start" event.
//
// Why: codex (translator.go emitToolStart/emitToolEnd) sends `arguments` only
// on "start" and `result` only on "end" — same for the other CLI runtimes.
// cameraSnapshotURL needs BOTH, so on those backends it never fired: every
// agent-initiated snapshot was written to disk but never surfaced in Flow
// Monitor. OpenClaw repeats the args on "end", which is why this went unnoticed.
func (h *AgentHandler) rememberToolArgs(callID, args string) {
	if callID == "" || args == "" {
		return
	}
	h.toolArgsMu.Lock()
	defer h.toolArgsMu.Unlock()
	if h.toolArgsByCall == nil {
		h.toolArgsByCall = make(map[string]string)
	}
	if len(h.toolArgsByCall) >= maxPendingToolArgs {
		clear(h.toolArgsByCall)
	}
	h.toolArgsByCall[callID] = args
}

// snapshotURLForToolCall resolves the snapshot URL for a tool event, preferring
// the args carried by this event and falling back to the ones remembered from
// the matching "start".
//
// The empty-result guard is load-bearing, not a shortcut: this runs on the
// "start" event too, and consuming the map there would delete the args that the
// matching "end" event is about to need — the exact bug this function exists to
// fix. A snapshot URL needs a result, so no result means nothing to resolve yet.
func (h *AgentHandler) snapshotURLForToolCall(callID, toolArgs, result string) string {
	if result == "" {
		return ""
	}
	if u := cameraSnapshotURL(toolArgs, result); u != "" {
		return u
	}
	return cameraSnapshotURL(h.takeToolArgs(callID), result)
}

func (h *AgentHandler) takeToolArgs(callID string) string {
	if callID == "" {
		return ""
	}
	h.toolArgsMu.Lock()
	defer h.toolArgsMu.Unlock()
	args := h.toolArgsByCall[callID]
	delete(h.toolArgsByCall, callID)
	return args
}
