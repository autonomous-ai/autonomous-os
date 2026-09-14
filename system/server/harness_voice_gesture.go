package server

import (
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/server/serializers"
)

// handleHarnessVoiceGesture is called by HAL's action worker, never by the
// input poller. TTS stays in HAL so configured-language button feedback uses
// the same phrase and playback pipeline as other physical controls.
func (s *Server) handleHarnessVoiceGesture(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	var req struct {
		GestureID string `json:"gestureId" binding:"required,uuid"`
	}
	if err := bindHarnessVoiceJSON(c, &req); err != nil {
		return
	}
	if s.harnessVoice == nil {
		writeHarnessGestureError(c, http.StatusServiceUnavailable, "harness_offline", "Harness voice service unavailable")
		return
	}
	state, err := s.harnessVoice.ToggleGesture(c.Request.Context(), req.GestureID)
	if err != nil {
		code := "focus_unavailable"
		var actionError *harness.VoiceGestureError
		if errors.As(err, &actionError) {
			code = actionError.Code
		}
		writeHarnessGestureError(c, http.StatusConflict, code, err.Error())
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(state))
}
func writeHarnessGestureError(c *gin.Context, status int, code, message string) {
	c.JSON(status, gin.H{"status": 0, "data": gin.H{"code": code}, "message": message})
}
