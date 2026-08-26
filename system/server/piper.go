package server

import (
	"encoding/json"
	"io"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/server/serializers"
)

// Piper install proxy. The admin page needs three calls that only HAL can
// serve (it owns /opt/piper), but the browser can only reach os-server — HAL
// listens on loopback. These forward verbatim rather than re-modelling the
// payloads, so the catalogue stays defined in exactly one place: HAL's
// piper_catalog.py.
//
// Admin-gated: installing software and writing 63 MB to the device is not
// something an unauthenticated LAN caller should be able to trigger.

// piperProxy forwards one request to HAL and copies the reply back.
func piperProxy(c *gin.Context, method, path string) {
	var body io.Reader
	if method == http.MethodPost {
		body = c.Request.Body
	}
	req, err := http.NewRequestWithContext(c.Request.Context(), method, hal.BaseURL+path, body)
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	req.Header.Set("Content-Type", "application/json")
	// Downloads run on a HAL background thread and the handler returns at once,
	// so this timeout only covers the handshake, never the transfer.
	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("hal unreachable: "+err.Error()))
		return
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	// HAL speaks plain JSON; the web client only accepts this app's
	// {status,data,message} envelope and reads anything else as a failed
	// request. Wrap rather than teach the client a second shape.
	var payload any
	if err := json.Unmarshal(raw, &payload); err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("hal returned invalid JSON: "+err.Error()))
		return
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		c.JSON(resp.StatusCode, serializers.ResponseError(string(raw)))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(payload))
}

func (s *Server) piperStatus(c *gin.Context)  { piperProxy(c, http.MethodGet, "/voice/piper/status") }
func (s *Server) piperInstall(c *gin.Context) { piperProxy(c, http.MethodPost, "/voice/piper/install") }
func (s *Server) piperVoice(c *gin.Context)   { piperProxy(c, http.MethodPost, "/voice/piper/voice") }
