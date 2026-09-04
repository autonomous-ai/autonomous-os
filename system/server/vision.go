package server

import (
	"encoding/base64"
	"net/http"
	"os"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/vision"
)

// lookWidth/lookQuality shrink the JPEG (~50-80 KB instead of ~300-500 KB at
// full 1920x1080) so the vision model uploads and tokenizes faster. 768 px wide
// still reads text on a laptop screen and recognizes people and objects.
// Server-side so the agent cannot drop them by paraphrasing the skill.
const (
	lookWidth   = 768
	lookQuality = 75
)

// lookRequest is the body of POST /api/vision/look.
type lookRequest struct {
	// Question is what the user asked, so the vision model answers it instead
	// of narrating the frame generically. Optional.
	Question string `json:"question"`
}

// lookAndDescribe captures a frame and hands back text the agent can actually
// read — one call, no branching for the agent to get wrong.
//
// Why this exists: the describe-first gate in the sensing handler only covers
// images that enter a turn from OUTSIDE (a chat/Telegram attachment, HAL's
// realtime look-frame handoff). When the agent looks around DURING a turn —
// "turn right and tell me what you see" — HAL's /camera/snapshot hands back
// only `{"path": ...}`, and a text-only main model (Auto-AI) can never see the
// file. Observed on lamp-0c89 2026-09-04: the agent aimed the servo, saved a
// valid frame, then invented a description of it ("pretty dark, just shadows").
//
// The vision-capability branch lives HERE rather than in the skill: when the
// main model can read images itself, describing would be a wasted 8-38s round
// trip that also throws away detail, but that is the server's business, not
// something the agent should have to decide mid-turn. It gets `description`
// when it needs one and `path` either way.
//
// Loopback-only: the caller is the agent's own shell tool on the device, and
// the call moves hardware and spends a vision-model call.
func (s *Server) lookAndDescribe(c *gin.Context) {
	var req lookRequest
	// Body is optional — a bare POST is a valid "just look".
	_ = c.ShouldBindJSON(&req)

	path, err := hal.Snapshot(lookWidth, lookQuality)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("snapshot failed: "+err.Error()))
		return
	}
	if vision.ModelSupportsVision(s.config) {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"path": path}))
		return
	}
	data, err := os.ReadFile(path)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("snapshot not readable"))
		return
	}
	desc, err := vision.DescribeWithRetry(s.config, base64.StdEncoding.EncodeToString(data), req.Question)
	if err != nil {
		// Fail LOUD, not with an empty description: the skill tells the agent to
		// admit it could not see rather than guess, which is the whole point.
		c.JSON(http.StatusBadGateway, serializers.ResponseError("describe failed: "+err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"path": path, "description": desc}))
}
