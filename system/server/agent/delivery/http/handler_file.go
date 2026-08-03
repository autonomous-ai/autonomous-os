package http

import (
	"errors"
	"net/http"
	"path/filepath"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/agentfile"
)

// Serving a device-local file the agent produced, so the web chat can SHOW it.
//
// The chat can already send a file INTO a turn (base64 on POST /sensing/event),
// but there was no way back out: ask the agent for a photo and it answers with
// an absolute path — `/root/.openclaw/media/hal-snapshots/snap_*.jpg` — which a
// browser cannot read. The web detects such a path in a reply and points an
// <img>/download at this endpoint.
//
// What may leave the device is decided in system/agentfile, NOT here: the MQTT
// path that serves the same files to a phone (kind chat.file.get) has to agree
// with this one exactly, and two copies of an allow-list is two chances to widen
// one by accident. This handler is only the HTTP dressing.
//
// A narrower, path-free variant already exists for Flow Monitor tool results
// (SensingHandler.GetAgentSnapshot, which takes runtime/source/name segments
// instead). That one stays as is: it serves what the DEVICE resolved, while this
// serves what the AGENT named.

// ServeFile handles GET /api/agent/file?path=<absolute path>.
//
// The client sends the RAW PATH, so it is treated as hostile input — see
// agentfile.Resolve for the two gates.
//
// Bare status codes rather than the usual JSON envelope: the only caller is an
// <img> / download link, which reads the status and nothing else.
func (h *AgentHandler) ServeFile(c *gin.Context) {
	path, contentType, err := agentfile.Resolve(c.Query("path"), agentfile.Roots())
	if err != nil {
		switch {
		case errors.Is(err, agentfile.ErrType), errors.Is(err, agentfile.ErrOutsideRoots):
			c.Status(http.StatusForbidden)
		default:
			c.Status(http.StatusNotFound)
		}
		return
	}

	disposition := "attachment"
	if agentfile.Inline(path) {
		disposition = "inline"
	}
	// filename is the basename only — the full path is already known to this
	// caller, but nothing downstream should re-derive a directory from a header.
	c.Header("Content-Disposition", disposition+`; filename="`+filepath.Base(path)+`"`)
	c.Header("Content-Type", contentType)
	// The whitelist decides the type; never let a sniffed one override it.
	c.Header("X-Content-Type-Options", "nosniff")
	c.File(path)
}
