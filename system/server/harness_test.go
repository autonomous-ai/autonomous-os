package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/server/config"
)

func TestHarnessRoutesProtectCodeAndLocalCommands(t *testing.T) {
	service, err := harness.NewService(t.TempDir(), harness.Callbacks{})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	service.Start(ctx)
	s := &Server{config: &config.Config{LLMAPIKey: "test-owner-token"}, harnessService: service}
	router := gin.New()
	s.registerHarnessRoutes(router.Group("/api"), ctx)
	call := func(method, path, address, token, forwarded string) *httptest.ResponseRecorder {
		t.Helper()
		req := httptest.NewRequest(method, path, nil)
		req.RemoteAddr = address
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		if forwarded != "" {
			req.Header.Set("X-Forwarded-For", forwarded)
		}
		out := httptest.NewRecorder()
		router.ServeHTTP(out, req)
		return out
	}
	for _, path := range []string{"/api/harness/pair", "/api/harness/pair/status"} {
		method := http.MethodGet
		if path == "/api/harness/pair" {
			method = http.MethodPost
		}
		if out := call(method, path, "192.168.1.20:1234", "", ""); out.Code != http.StatusUnauthorized {
			t.Fatalf("unauthorized %s: %d", path, out.Code)
		}
	}
	out := call(http.MethodPost, "/api/harness/pair", "192.168.1.20:1234", "test-owner-token", "")
	if out.Code != http.StatusAccepted || out.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("generate without machine ID: %d %s", out.Code, out.Body.String())
	}
	var generated struct {
		Data harness.PairInfo `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &generated); err != nil || len(generated.Data.Code) != 6 {
		t.Fatalf("device must generate the code: %v", err)
	}
	out = call(http.MethodGet, "/api/harness/status", "127.0.0.1:1234", "", "")
	var status struct {
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &status); err != nil || out.Code != http.StatusOK {
		t.Fatalf("local status: %d %v", out.Code, err)
	}
	if _, leaked := status.Data["code"]; leaked {
		t.Fatal("public connection status leaked pairing code")
	}
	for _, address := range []string{"192.168.1.20:1234", "127.0.0.1:1234"} {
		out = call(http.MethodPost, "/api/harness/request", address, "test-owner-token", "192.168.1.20")
		if out.Code != http.StatusForbidden {
			t.Fatalf("LAN/proxied agent request admitted: %d", out.Code)
		}
	}
	out = call(http.MethodPost, "/api/harness/pair/cancel", "192.168.1.20:1234", "test-owner-token", "")
	if out.Code != http.StatusOK || service.PairStatus().Code != "" {
		t.Fatalf("cancel did not clear code: %d", out.Code)
	}
}
