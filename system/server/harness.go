package server

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/server/serializers"
)

// registerHarnessRoutes exposes management to the owner and commands only to the device runtime.
func (s *Server) registerHarnessRoutes(api *gin.RouterGroup, ctx context.Context) {
	group := api.Group("harness")
	group.Use(func(c *gin.Context) {
		if s.harnessService == nil {
			c.AbortWithStatusJSON(http.StatusServiceUnavailable, serializers.ResponseError("Harness service unavailable"))
		}
	})
	group.GET("status", adminOrLoopbackAuth(s.config), func(c *gin.Context) {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(s.harnessService.Status()))
	})
	// Pairing codes and pinned E2EE identities authenticate the direct socket.
	group.GET("ws", func(c *gin.Context) {
		s.harnessService.ServeHTTP(c.Writer, c.Request)
	})
	group.GET("pair/status", adminAuthMiddleware(s.config), func(c *gin.Context) {
		c.Header("Cache-Control", "no-store")
		c.JSON(http.StatusOK, serializers.ResponseSuccess(s.harnessService.PairStatus()))
	})
	group.POST("pair", adminAuthMiddleware(s.config), func(c *gin.Context) {
		info, err := s.harnessService.StartPair(ctx)
		if err != nil {
			c.JSON(http.StatusConflict, serializers.ResponseError(err.Error()))
			return
		}
		c.Header("Cache-Control", "no-store")
		c.JSON(http.StatusAccepted, serializers.ResponseSuccess(info))
	})
	group.POST("pair/cancel", adminAuthMiddleware(s.config), func(c *gin.Context) {
		if err := s.harnessService.CancelPair(); err != nil {
			c.JSON(http.StatusInternalServerError, serializers.ResponseError("Could not cancel Harness pairing"))
			return
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"cancelled": true}))
	})
	group.DELETE("", adminAuthMiddleware(s.config), func(c *gin.Context) {
		if err := s.harnessService.Unpair(); err != nil {
			c.JSON(http.StatusInternalServerError, serializers.ResponseError("Could not remove Harness pairing"))
			return
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"unpaired": true}))
	})
	group.POST("request", localOnlyMiddleware(), func(c *gin.Context) {
		var frame harness.Frame
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 64*1024)
		if err := c.ShouldBindJSON(&frame); err != nil || frame == nil {
			c.JSON(http.StatusBadRequest, serializers.ResponseError("Invalid Harness request"))
			return
		}
		requestCtx, cancel := context.WithTimeout(c.Request.Context(), 35*time.Second)
		defer cancel()
		result, err := s.harnessService.Request(requestCtx, frame)
		if err != nil {
			var uncertain *harness.DeliveryUnknownError
			if errors.As(err, &uncertain) {
				c.JSON(http.StatusBadGateway, serializers.ResponseError("Harness delivery is unknown; inspect receipt before sending again"))
				return
			}
			c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
			return
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(result))
	})
}

// forwardHarnessEvent speaks the completed Harness result itself. A summary is
// already untrusted display data from the paired computer; sending its JSON
// through the device agent created a second, unrelated agent turn and hid the
// actual result behind an "Sent" response in Web Chat.
func (s *Server) forwardHarnessEvent(frame harness.Frame) {
	text := harnessSummaryText(frame)
	if text == "" {
		return
	}
	go func() {
		if err := hal.SpeakReply(text); err != nil {
			slog.Warn("speak Harness result failed", "component", "harness", "error", err)
		}
	}()
}

func harnessSummaryText(frame harness.Frame) string {
	if kind, _ := frame["kind"].(string); kind != "turn.summary" {
		return ""
	}
	payload, _ := frame["payload"].(map[string]any)
	if payload == nil {
		return ""
	}
	text, _ := payload["text"].(string)
	if strings.TrimSpace(text) == "" {
		text, _ = payload["recap"].(string)
	}
	text = strings.TrimSpace(text)
	const maxRunes = 3000
	if len([]rune(text)) > maxRunes {
		text = string([]rune(text)[:maxRunes]) + "…"
	}
	return text
}
