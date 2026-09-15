package server

import (
	"context"
	"crypto/sha256"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/runtimes/intern"
	"go.autonomous.ai/os/system/lib/internbridge"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/server/session"
)

const internVoiceMaxSeconds = 300

var internTranscriptAccessGrant = regexp.MustCompile(`(?i)(grant|allow|authorize|permit)(?:[[:space:][:punct:]]+\w+){0,5}[[:space:][:punct:]]+(restricted|secret)(?:[[:space:][:punct:]]+\w+){0,3}[[:space:][:punct:]]+access|(restricted|secret)[[:space:][:punct:]]+access(?:[[:space:][:punct:]]+\w+){0,5}[[:space:][:punct:]]+(grant|allow|authorize|permit)`)

// One ephemeral grant per router lifecycle. No token, transcript or persistent
// configuration is retained. Replacement invalidates work under the old grant.
type internVoiceGate struct {
	lifetime context.Context
	mu       sync.Mutex
	owner    [32]byte
	ctx      context.Context
	cancel   context.CancelFunc
}

// Require an existing signed admin session, never the legacy provider-key
// bearer fallback. Use a bearer session explicitly; no ambient cookie grant.
func (s *Server) internVoiceSession(c *gin.Context) ([32]byte, time.Time, bool) {
	token := strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer ")
	if !strings.HasPrefix(c.GetHeader("Authorization"), "Bearer ") || !session.VerifyToken(token, s.config) {
		c.JSON(403, serializers.ResponseError("signed administrator session required"))
		return [32]byte{}, time.Time{}, false
	}
	// VerifyToken has authenticated the existing <expiry>.<signature> format.
	exp, err := strconv.ParseInt(strings.SplitN(token, ".", 2)[0], 10, 64)
	if err != nil {
		c.JSON(403, serializers.ResponseError("signed administrator session required"))
		return [32]byte{}, time.Time{}, false
	}
	return sha256.Sum256([]byte(token)), time.Unix(exp, 0), true
}

func (s *Server) internVoiceGrant(g *internVoiceGate) gin.HandlerFunc {
	return func(c *gin.Context) {
		owner, sessionExpiry, ok := s.internVoiceSession(c)
		if !ok {
			return
		}
		var req struct {
			Seconds   int    `json:"expires_in_seconds"`
			Admission string `json:"admission"`
		}
		if decodeInternBody(c, &req) != nil || req.Seconds < 1 || req.Seconds > internVoiceMaxSeconds || req.Admission != "administrator_grants_voice_session" {
			c.JSON(400, serializers.ResponseError("explicit voice grant with expiry of 1 to 300 seconds required"))
			return
		}
		expires := time.Now().Add(time.Duration(req.Seconds) * time.Second)
		if sessionExpiry.Before(expires) {
			expires = sessionExpiry
		}
		g.mu.Lock()
		if g.cancel != nil {
			g.cancel()
		}
		g.owner = owner
		g.ctx, g.cancel = context.WithDeadline(g.lifetime, expires)
		g.mu.Unlock()
		c.JSON(200, serializers.ResponseSuccess(gin.H{"expires_at": expires.UTC(), "scope": "classified_transcripts_only"}))
	}
}

func (s *Server) internVoiceRevoke(g *internVoiceGate) gin.HandlerFunc {
	return func(c *gin.Context) {
		if _, _, ok := s.internVoiceSession(c); !ok {
			return
		}
		// Any authenticated administrator can disable the single active grant.
		g.mu.Lock()
		if g.cancel != nil {
			g.cancel()
		}
		g.ctx, g.cancel, g.owner = nil, nil, [32]byte{}
		g.mu.Unlock()
		c.JSON(200, serializers.ResponseSuccess(gin.H{"voice_enabled": false}))
	}
}

func (s *Server) internVoiceChat(g *internVoiceGate) gin.HandlerFunc {
	return func(c *gin.Context) {
		owner, _, ok := s.internVoiceSession(c)
		if !ok {
			return
		}
		g.mu.Lock()
		ctx := g.ctx
		allowed := ctx != nil && ctx.Err() == nil && g.owner == owner
		g.mu.Unlock()
		if !allowed {
			c.JSON(403, serializers.ResponseError("active voice session grant required"))
			return
		}
		var req struct {
			Text      string                 `json:"text"`
			Operation internbridge.Operation `json:"operation"`
			DataClass internbridge.DataClass `json:"data_class"`
			Admission string                 `json:"admission"`
		}
		if decodeInternBody(c, &req) != nil || req.Admission != "administrator_classified_exact_text" {
			c.JSON(400, serializers.ResponseError("explicit administrator classification required"))
			return
		}
		a, err := intern.AdmitTrustedVoiceRequest(ctx, internbridge.Request{Text: req.Text, Operation: req.Operation, DataClass: req.DataClass})
		if err != nil {
			c.JSON(400, serializers.ResponseError(err.Error()))
			return
		}
		gw, ok := s.agentGateway.(*intern.Service)
		if !ok {
			c.JSON(409, serializers.ResponseError("intern is not active"))
			return
		}
		id, err := gw.Submit(a)
		if err != nil {
			c.JSON(503, serializers.ResponseError(err.Error()))
			return
		}
		c.JSON(202, serializers.ResponseSuccess(gin.H{"run_id": id, "state": "queued", "scope": "bridge_request"}))
	}
}

// internVoiceTranscript admits only an administrator-classified final STT
// transcript. Wake detection is evidence about addressing, never custody.
func (s *Server) internVoiceTranscript(g *internVoiceGate) gin.HandlerFunc {
	return func(c *gin.Context) {
		owner, _, ok := s.internVoiceSession(c)
		if !ok {
			return
		}
		g.mu.Lock()
		ctx := g.ctx
		allowed := ctx != nil && ctx.Err() == nil && g.owner == owner
		g.mu.Unlock()
		if !allowed {
			c.JSON(403, serializers.ResponseError("active voice session grant required"))
			return
		}
		var req struct {
			Transcript string                 `json:"transcript"`
			Final      bool                   `json:"final"`
			WakeWord   string                 `json:"wake_word"`
			Operation  internbridge.Operation `json:"operation"`
			DataClass  internbridge.DataClass `json:"data_class"`
			Admission  string                 `json:"admission"`
		}
		if decodeInternBody(c, &req) != nil || !req.Final || !strings.EqualFold(req.WakeWord, "Gus") ||
			req.Operation != internbridge.Reception || (req.DataClass != internbridge.Public && req.DataClass != internbridge.Business) ||
			req.Admission != "administrator_final_transcript" || internTranscriptAccessGrant.MatchString(req.Transcript) {
			c.JSON(400, serializers.ResponseError("explicit final Gus transcript admission required"))
			return
		}
		a, err := intern.AdmitTrustedVoiceRequest(ctx, internbridge.Request{Text: req.Transcript, Operation: req.Operation, DataClass: req.DataClass})
		if err != nil {
			c.JSON(400, serializers.ResponseError(err.Error()))
			return
		}
		gw, ok := s.agentGateway.(*intern.Service)
		if !ok {
			c.JSON(409, serializers.ResponseError("intern is not active"))
			return
		}
		id, err := gw.Submit(a)
		if err != nil {
			c.JSON(503, serializers.ResponseError(err.Error()))
			return
		}
		c.JSON(202, serializers.ResponseSuccess(gin.H{"run_id": id, "state": "queued", "scope": "bridge_request"}))
	}
}
