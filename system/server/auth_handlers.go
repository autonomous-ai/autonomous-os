package server

import (
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/server/session"
)

// loginHandler validates the admin password and issues a session cookie.
// POST /api/login  body: {"password": "..."}.
//
// Returns 401 on any failure (no password set, wrong password, malformed
// hash). Uniform error keeps the response from leaking which case fired.
func (s *Server) loginHandler(c *gin.Context) {
	var body struct {
		Password string `json:"password"`
	}
	if err := c.ShouldBindJSON(&body); err != nil || body.Password == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("password required"))
		return
	}
	if err := s.deviceService.VerifyAdminPassword(body.Password); err != nil {
		slog.Info("login rejected", "component", "auth", "error", err)
		c.JSON(http.StatusUnauthorized, serializers.ResponseError("invalid credentials"))
		return
	}
	if err := session.Issue(c, s.config); err != nil {
		slog.Error("issue session failed", "component", "auth", "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("session issue failed"))
		return
	}
	// Return token in body for cross-origin apps that can't use the cookie.
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"token": session.LatestToken(c)}))
}

// logoutHandler clears the session cookie. Stateless tokens mean we can't
// actively revoke server-side; the client losing the cookie is enough.
// Anyone who already exfiltrated the token can still use it until expiry.
func (s *Server) logoutHandler(c *gin.Context) {
	session.Clear(c)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// loginExchangeHandler mints a session cookie for an already-authed
// adminAuthMiddleware request. No body — the cookie is bound to the
// response origin, which is exactly the property we need for the AP→.local
// post-setup handoff.
func (s *Server) loginExchangeHandler(c *gin.Context) {
	if err := session.Issue(c, s.config); err != nil {
		slog.Error("exchange session failed", "component", "auth", "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("session issue failed"))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}
