package server

import (
	"net/http"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/server/serializers"
)

// environmentStatus lets local agent tools read diagnostic snapshots without
// reading credentials. The route is protected by localOnlyMiddleware.
func (s *Server) environmentAvailable() bool {
	return device.Capabilities(s.config.DeviceTypeOrDefault())[device.CapEnvironment]
}

func (s *Server) environmentStatus(c *gin.Context) {
	if !s.environmentAvailable() {
		c.JSON(http.StatusForbidden, serializers.ResponseError("environment capability not declared"))
		return
	}
	body, err := hal.GetEnvironmentStatusContext(c.Request.Context())
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(body))
}
