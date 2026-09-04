package http

import (
	"regexp"
	"strings"
)

// cameraSnapshotPathRE accepts JPEGs only from the active agent runtime's
// approved camera-output directories. The UI receives a server URL, never the
// runtime's filesystem path.
var cameraSnapshotPathRE = regexp.MustCompile(`/root/\.(openclaw|hermes|picoclaw|codex|claudecode)/(workspace|media/hal-snapshots)/([A-Za-z0-9][A-Za-z0-9._-]*\.(jpg|jpeg))\b`)

// cameraSnapshotURL returns the UI-safe URL for a snapshot produced by a
// camera tool call. Tool output is untrusted agent text, so both the camera
// command and an approved runtime path must match before exposing anything.
func cameraSnapshotURL(toolArgs, result string) string {
	// Both ways the agent can capture a frame: HAL's raw snapshot endpoint and
	// os-server's /api/vision/look (snapshot + describe). Missing the latter
	// would silently drop the thumbnail for every turn that follows the skill.
	if !strings.Contains(toolArgs, "/camera/snapshot") && !strings.Contains(toolArgs, "/api/vision/look") {
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
