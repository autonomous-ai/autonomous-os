package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
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

// A POST is retried while HAL is not listening, because saving any voice
// setting makes os-server restart HAL and a click landing in that window would
// otherwise be lost — the operator is told nothing changed and has to guess
// when to try again.
//
// Only a failed dial is retried, and the distinction matters: a dial that never
// connected proves the request was not delivered, so replaying it cannot repeat
// an effect. A timeout proves nothing of the sort — the deadline covers reading
// the reply, so HAL may well have done the work and simply answered slowly.
// Those surface as a plain failure instead.
const (
	piperRetryWindow   = 25 * time.Second
	piperRetryInterval = time.Second
)

// dialFailed reports whether the request never reached HAL — nothing was
// connected to, so nothing could have been acted on.
func dialFailed(err error) bool {
	var opErr *net.OpError
	return errors.As(err, &opErr) && opErr.Op == "dial"
}

// piperFetch performs one request, retrying only while nothing answers.
//
// Split out from the handler so the retry can be tested against a listener
// that goes away and comes back, which is precisely what a HAL restart is.
func piperFetch(ctx context.Context, method, url string, reqBody []byte, deadline time.Time) (*http.Response, error) {
	// Per attempt, not per call. Downloads run in their own process and the HAL
	// handler returns at once, so this only ever covers the handshake.
	client := &http.Client{Timeout: 10 * time.Second}
	for {
		var body io.Reader
		if reqBody != nil {
			body = bytes.NewReader(reqBody)
		}
		req, err := http.NewRequestWithContext(ctx, method, url, body)
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := client.Do(req)
		if err == nil {
			return resp, nil
		}
		// GET is left to fail fast: the admin page polls status every few
		// seconds and uses the failure to show that the device is restarting.
		// Holding those open would stack up requests and hide the state.
		if method != http.MethodPost || !dialFailed(err) ||
			!time.Now().Before(deadline) || ctx.Err() != nil {
			return nil, err
		}
		time.Sleep(piperRetryInterval)
	}
}

// piperProxy forwards one request to HAL and copies the reply back.
func piperProxy(c *gin.Context, method, path string) {
	var reqBody []byte
	if method == http.MethodPost {
		reqBody, _ = io.ReadAll(c.Request.Body)
	}
	resp, err := piperFetch(c.Request.Context(), method, hal.BaseURL+path, reqBody,
		time.Now().Add(piperRetryWindow))
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
func (s *Server) piperVoiceRemove(c *gin.Context) {
	piperProxy(c, http.MethodPost, "/voice/piper/voice/remove")
}
