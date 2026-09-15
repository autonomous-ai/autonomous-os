package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/runtimes/intern"
	"go.autonomous.ai/os/system/agent"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/internbridge"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/server/serializers"
)

// InitializeInternServer is checked before loading HAL's environment, logger,
// device constructors or Wire's normal provisioning graph. Load is read-only:
// it does not run ProvideConfig's migration or voice/config seeding.
func InitializeServer() (*Server, error) {
	if s, selected, err := InitializeInternServer(); selected || err != nil {
		return s, err
	}
	return initializeDeviceServer()
}

func InitializeInternServer() (*Server, bool, error) {
	cfg, err := config.Load()
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	if !domain.IsExternallyOwnedRuntime(cfg.AgentRuntimeValue()) {
		return nil, false, nil
	}
	if cfg.HttpPort == 0 {
		cfg.HttpPort = config.Default().HttpPort
	}
	return newInternServer(cfg), true, nil
}

func newInternServer(cfg *config.Config) *Server {
	gw := agent.ProvideGateway(cfg, nil, nil)
	return &Server{config: cfg, agentGateway: gw, deviceService: device.ProvideService(cfg, nil, gw, nil, nil)}
}

// internRouter deliberately has no hardware proxy, channels, files, skills,
// sensing, login provisioning, MQTT, environment capture, or device routes.
// Existing administrator sessions authenticate classifications and result reads;
// no bridge credentials are loaded or forwarded. No body/query logging.
func (s *Server) internRouter() *gin.Engine {
	r := gin.New()
	r.Use(gin.Recovery())
	api := r.Group("/api", s.internAdmin())
	api.POST("/agent/intern/chat", s.internChat)
	api.GET("/agent/intern/result/:runID", s.internResult)
	api.GET("/agent/status", func(c *gin.Context) {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
			"runtime": s.agentGateway.Name(), "ready": s.agentGateway.IsReady(),
			"readiness_scope": "recent_bridge_generation", "version": s.agentGateway.Version(),
			"busy": s.agentGateway.IsBusy(), "executes_actions": false,
		}))
	})
	api.GET("/device/agent-runtime", func(c *gin.Context) {
		status := s.config.RuntimeSelectionStatus(s.agentGateway.Name(), s.agentGateway.IsReady())
		status["options"] = domain.AgentRuntimes
		c.JSON(http.StatusOK, serializers.ResponseSuccess(status))
	})
	api.POST("/device/agent-runtime", func(c *gin.Context) {
		var req domain.AgentRuntimeSetData
		if decodeInternBody(c, &req) != nil {
			c.JSON(400, serializers.ResponseError("invalid request"))
			return
		}
		if err := s.deviceService.SelectExternalRuntime(req); err != nil {
			c.JSON(400, serializers.ResponseError(err.Error()))
			return
		}
		status := s.config.RuntimeSelectionStatus(s.agentGateway.Name(), s.agentGateway.IsReady())
		status["services_changed"] = false
		c.JSON(200, serializers.ResponseSuccess(status))
	})
	r.NoRoute(func(c *gin.Context) {
		c.JSON(http.StatusNotImplemented, serializers.ResponseError(domain.ErrNotSupportedByRuntime.Error()))
	})
	return r
}

// Classification authority is an existing authenticated administrator, not a
// LAN/browser origin, forwarded header or model. Cross-origin and query-token
// admission are excluded. Admins explicitly assert the class of the exact body.
func (s *Server) internAdmin() gin.HandlerFunc {
	auth := adminAuthMiddleware(s.config)
	return func(c *gin.Context) {
		if c.GetHeader("Origin") != "" || c.Request.URL.RawQuery != "" {
			c.AbortWithStatusJSON(http.StatusForbidden, serializers.ResponseError("intern requires direct administrator admission"))
			return
		}
		auth(c)
	}
}

func decodeInternBody(c *gin.Context, dst any) error {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, internbridge.MaxRequestBytes)
	raw, err := io.ReadAll(c.Request.Body)
	if err != nil || !utf8.Valid(raw) || !validInternSurrogates(raw) {
		return internbridge.ErrInvalidRequest
	}
	// JSON's last-key-wins behavior must not change an admission assertion.
	check := json.NewDecoder(bytes.NewReader(raw))
	if token, err := check.Token(); err != nil || token != json.Delim('{') {
		return internbridge.ErrInvalidRequest
	}
	seen := make(map[string]bool)
	for check.More() {
		token, err := check.Token()
		key, ok := token.(string)
		if err != nil || !ok || seen[key] || key != strings.ToLower(key) {
			return internbridge.ErrInvalidRequest
		}
		seen[key] = true
		var value json.RawMessage
		if check.Decode(&value) != nil {
			return internbridge.ErrInvalidRequest
		}
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(dst); err != nil {
		return err
	}
	if d.Decode(new(any)) != io.EOF {
		return internbridge.ErrInvalidRequest
	}
	return nil
}

// Reject escapes encoding unpaired UTF-16 surrogates before encoding/json can
// replace them with U+FFFD. Escaped backslashes are literal text, not escapes.
func validInternSurrogates(raw []byte) bool {
	for i := 0; i < len(raw); i++ {
		if raw[i] != '\\' {
			continue
		}
		i++
		if i >= len(raw) {
			return false
		}
		if raw[i] != 'u' {
			continue
		}
		if i+4 >= len(raw) {
			return false
		}
		code, err := strconv.ParseUint(string(raw[i+1:i+5]), 16, 16)
		if err != nil {
			return false
		}
		i += 4
		if code >= 0xdc00 && code <= 0xdfff {
			return false
		}
		if code < 0xd800 || code > 0xdbff {
			continue
		}
		if i+6 >= len(raw) || raw[i+1] != '\\' || raw[i+2] != 'u' {
			return false
		}
		low, err := strconv.ParseUint(string(raw[i+3:i+7]), 16, 16)
		if err != nil || low < 0xdc00 || low > 0xdfff {
			return false
		}
		i += 6
	}
	return true
}

func (s *Server) internChat(c *gin.Context) {
	gw, ok := s.agentGateway.(*intern.Service)
	if !ok {
		c.JSON(409, serializers.ResponseError("intern is not active"))
		return
	}
	var req struct {
		Text      string                 `json:"text"`
		Operation internbridge.Operation `json:"operation"`
		DataClass internbridge.DataClass `json:"data_class"`
		Assertion string                 `json:"admission"`
	}
	if decodeInternBody(c, &req) != nil || req.Assertion != "administrator_classified_exact_text" {
		c.JSON(400, serializers.ResponseError("explicit administrator classification required"))
		return
	}
	a, err := intern.AdmitTrustedRequest(internbridge.Request{Text: req.Text, Operation: req.Operation, DataClass: req.DataClass})
	if err != nil {
		c.JSON(400, serializers.ResponseError(err.Error()))
		return
	}
	id, err := gw.Submit(a)
	if err != nil {
		c.JSON(503, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusAccepted, serializers.ResponseSuccess(map[string]string{"run_id": id, "state": "queued", "scope": "bridge_request"}))
}

func (s *Server) internResult(c *gin.Context) {
	gw, ok := s.agentGateway.(*intern.Service)
	if !ok {
		c.JSON(409, serializers.ResponseError("intern is not active"))
		return
	}
	result, err := gw.Result(c.Param("runID"))
	if err != nil {
		c.JSON(404, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(200, serializers.ResponseSuccess(result))
}

func (s *Server) serveIntern(closeFn func()) error {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	defer closeFn()
	done := make(chan struct{})
	go func() { defer close(done); s.agentGateway.StartWS(ctx, nil) }()
	if waiter, ok := s.agentGateway.(interface{ WaitStarted(context.Context) error }); ok {
		if err := waiter.WaitStarted(ctx); err != nil {
			cancel()
			<-done
			return err
		}
	}
	srv := &http.Server{Addr: fmt.Sprintf("127.0.0.1:%d", s.config.HttpPort), Handler: s.internRouter(),
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
	shutdownDone := make(chan struct{})
	go func() {
		defer close(shutdownDone)
		<-ctx.Done()
		shutdownCtx, stop := context.WithTimeout(context.Background(), 5*time.Second)
		defer stop()
		_ = srv.Shutdown(shutdownCtx)
	}()
	err := srv.ListenAndServe()
	cancel()
	<-done
	<-shutdownDone
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}
