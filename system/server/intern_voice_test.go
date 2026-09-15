package server

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.autonomous.ai/os/runtimes/intern"
	"go.autonomous.ai/os/runtimes/intern/bridge"
	"go.autonomous.ai/os/system/lib/internbridge"
	"go.autonomous.ai/os/system/server/config"
)

const voicePath = "/api/agent/intern/voice/"
const voiceBody = `{"text":"Gus, Rex, draft a business greeting.","operation":"generate","data_class":"business","admission":"administrator_classified_exact_text"}`
const transcriptBody = `{"transcript":"Please welcome today's visitor.","final":true,"wake_word":"Gus","operation":"reception","data_class":"business","admission":"administrator_final_transcript"}`

func voiceSessionToken(cfg *config.Config, expiry time.Time) string {
	key, _ := hex.DecodeString(cfg.SessionSecret)
	exp := fmt.Sprint(expiry.Unix())
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(exp))
	return exp + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func TestInternVoiceGrantBoundary(t *testing.T) {
	var calls atomic.Int32
	var block atomic.Bool
	entered, stopped := make(chan struct{}), make(chan struct{})
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var payload struct {
			Messages  []struct{ Role, Content string }
			Think     bool
			KeepAlive string `json:"keep_alive"`
		}
		if json.NewDecoder(r.Body).Decode(&payload) != nil || len(payload.Messages) != 2 || payload.Messages[1].Content != "Gus, Rex, draft a business greeting." || payload.Think || payload.KeepAlive != "5m" {
			t.Error("transcript, history or bounded defaults changed")
		}
		if r.Header.Get("Authorization") != "" || r.Header.Get("Cookie") != "" {
			t.Error("session leaked to provider")
		}
		if block.Load() {
			close(entered)
			<-r.Context().Done()
			close(stopped)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"message":{"role":"assistant","content":"Welcome to the business."},"done":true,"done_reason":"stop","eval_count":5}`)
	}))
	defer provider.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b, err := bridge.Start(ctx, bridge.ProviderConfig{Endpoint: provider.URL})
	if err != nil {
		t.Fatal(err)
	}
	defer b.Close(context.Background())
	cfg := &config.Config{AgentRuntime: "intern", SessionSecret: strings.Repeat("ab", 32), LLMAPIKey: "legacy-fixture"}
	s := newInternServer(cfg)
	w := s.agentGateway.(*intern.Service)
	done := make(chan struct{})
	go func() { defer close(done); w.StartWS(ctx, nil) }()
	defer func() { cancel(); <-done }()
	waitCtx, stop := context.WithTimeout(ctx, time.Second)
	defer stop()
	if err := w.WaitStarted(waitCtx); err != nil {
		t.Fatal(err)
	}
	r := s.internRouter()
	token := voiceSessionToken(cfg, time.Now().Add(time.Hour))
	other := voiceSessionToken(cfg, time.Now().Add(2*time.Hour))
	request := func(method, path, body, auth string, want int) *httptest.ResponseRecorder {
		t.Helper()
		rec := internRequest(t, r, method, voicePath+path, body, auth)
		if rec.Code != want {
			t.Fatalf("%s %s: %d want %d: %s", method, path, rec.Code, want, rec.Body.String())
		}
		return rec
	}
	grant := func(seconds int) {
		t.Helper()
		request("POST", "grant", fmt.Sprintf(`{"expires_in_seconds":%d,"admission":"administrator_grants_voice_session"}`, seconds), token, 200)
	}
	t.Cleanup(func() { internRequest(t, r, "DELETE", voicePath+"grant", "", token) })
	runID := func(rec *httptest.ResponseRecorder) string {
		t.Helper()
		var v struct {
			Data struct {
				RunID string `json:"run_id"`
			}
		}
		if json.Unmarshal(rec.Body.Bytes(), &v) != nil || v.Data.RunID == "" {
			t.Fatal("missing run ID")
		}
		return v.Data.RunID
	}
	t.Run("default_disabled_and_wake_only", func(t *testing.T) {
		request("POST", "chat", voiceBody, token, 403)
		request("POST", "chat", `{"text":"Gus"}`, token, 403)
		request("POST", "grant", `{"wake_word":"Gus"}`, token, 400)
		request("POST", "grant", `{"expires_in_seconds":300,"admission":"administrator_grants_voice_session"}`, "legacy-fixture", 403)
		request("POST", "grant", `{}`, "", 401)
		for _, n := range []int{0, -1, 301} {
			request("POST", "grant", fmt.Sprintf(`{"expires_in_seconds":%d,"admission":"administrator_grants_voice_session"}`, n), token, 400)
		}
	})
	grant(300)
	t.Run("classification_before_provider", func(t *testing.T) {
		for _, class := range []string{"restricted", "secret", "unknown", ""} {
			request("POST", "chat", strings.Replace(voiceBody, `"business"`, fmt.Sprintf("%q", class), 1), token, 400)
		}
		request("POST", "chat", strings.Replace(voiceBody, "administrator_classified_exact_text", "wake_word_detected", 1), token, 400)
		if calls.Load() != 0 || w.IsBusy() {
			t.Fatal("denied input reached queue/provider")
		}
	})
	t.Run("session_and_router_isolation", func(t *testing.T) {
		request("POST", "chat", voiceBody, other, 403)
		request("POST", "chat", voiceBody, voiceSessionToken(cfg, time.Now().Add(-time.Second)), 401)
		if rec := internRequest(t, s.internRouter(), "POST", voicePath+"chat", voiceBody, token); rec.Code != 403 {
			t.Fatal("grant leaked to new router")
		}
	})
	t.Run("valid_business_final_only", func(t *testing.T) {
		id := runID(request("POST", "chat", voiceBody, token, 202))
		turn := waitInternTurn(t, w, id)
		if turn.State != "completed" || turn.Result == nil || turn.Result.Output != "Welcome to the business." || turn.Result.Destination != internbridge.FirstContact || turn.ExecutesActions || calls.Load() != 1 {
			t.Fatalf("bad turn: %+v", turn)
		}
	})
	for _, mode := range []string{"revoked", "expired", "replaced"} {
		t.Run(mode+"_while_queued", func(t *testing.T) {
			w.SetBusy(true)
			seconds := 300
			if mode == "expired" {
				seconds = 1
			}
			grant(seconds)
			id := runID(request("POST", "chat", voiceBody, token, 202))
			switch mode {
			case "revoked":
				request("DELETE", "grant", "", other, 200)
			case "expired":
				time.Sleep(1100 * time.Millisecond)
			case "replaced":
				grant(300)
			}
			if mode != "replaced" {
				request("POST", "chat", voiceBody, token, 403)
			}
			w.SetBusy(false)
			turn := waitInternTurn(t, w, id)
			if turn.State != "canceled" || turn.Result != nil || turn.RemoteOutcomeUnknown || calls.Load() != 1 {
				t.Fatalf("invalid grant reached provider: %+v", turn)
			}
		})
	}
	t.Run("session_expiry_caps_grant", func(t *testing.T) {
		expires := time.Now().Add(2 * time.Second).Truncate(time.Second)
		short := voiceSessionToken(cfg, expires)
		rec := request("POST", "grant", `{"expires_in_seconds":300,"admission":"administrator_grants_voice_session"}`, short, 200)
		var v struct {
			Data struct {
				Expires time.Time `json:"expires_at"`
			}
		}
		if json.Unmarshal(rec.Body.Bytes(), &v) != nil || !v.Data.Expires.Equal(expires) {
			t.Fatal("grant outlived session")
		}
	})
	t.Run("revoke_in_flight", func(t *testing.T) {
		grant(300)
		block.Store(true)
		id := runID(request("POST", "chat", voiceBody, token, 202))
		select {
		case <-entered:
		case <-time.After(2 * time.Second):
			t.Fatal("provider not entered")
		}
		request("DELETE", "grant", "", token, 200)
		turn := waitInternTurn(t, w, id)
		if turn.State != "canceled" || turn.Result != nil || !turn.RemoteOutcomeUnknown {
			t.Fatalf("revocation did not cancel wait: %+v", turn)
		}
		select {
		case <-stopped:
		case <-time.After(2 * time.Second):
			t.Fatal("fixture provider not canceled")
		}
	})
}

func TestInternVoiceAdmissionRequiresBoundedGrant(t *testing.T) {
	r := internbridge.Request{Text: "business greeting", Operation: internbridge.Generate, DataClass: internbridge.Business}
	for _, ctx := range []context.Context{nil, context.Background()} {
		if _, err := intern.AdmitTrustedVoiceRequest(ctx, r); err == nil {
			t.Fatal("unbounded grant accepted")
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	cancel()
	if _, err := intern.AdmitTrustedVoiceRequest(ctx, r); err == nil {
		t.Fatal("canceled grant accepted")
	}
}

func TestInternVoiceLifecycleCancelsGrant(t *testing.T) {
	cfg := &config.Config{AgentRuntime: "intern", SessionSecret: strings.Repeat("ab", 32)}
	s := newInternServer(cfg)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	r := s.internRouterContext(ctx)
	token := voiceSessionToken(cfg, time.Now().Add(time.Hour))
	grant := `{"expires_in_seconds":300,"admission":"administrator_grants_voice_session"}`
	if rec := internRequest(t, r, "POST", voicePath+"grant", grant, token); rec.Code != 200 {
		t.Fatal(rec.Code)
	}
	cancel()
	if rec := internRequest(t, r, "POST", voicePath+"chat", voiceBody, token); rec.Code != 403 {
		t.Fatal("grant survived lifecycle", rec.Code)
	}
}

func TestInternFinalTranscriptAdmission(t *testing.T) {
	var calls atomic.Int32
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"message":{"role":"assistant","content":"orchestration"},"done":true,"done_reason":"stop","eval_count":1}`)
	}))
	defer provider.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b, err := bridge.Start(ctx, bridge.ProviderConfig{Endpoint: provider.URL})
	if err != nil {
		t.Fatal(err)
	}
	defer b.Close(context.Background())
	cfg := &config.Config{AgentRuntime: "intern", SessionSecret: strings.Repeat("cd", 32)}
	s := newInternServer(cfg)
	w := s.agentGateway.(*intern.Service)
	done := make(chan struct{})
	go func() { defer close(done); w.StartWS(ctx, nil) }()
	defer func() { cancel(); <-done }()
	waitCtx, stop := context.WithTimeout(ctx, time.Second)
	defer stop()
	if err := w.WaitStarted(waitCtx); err != nil {
		t.Fatal(err)
	}
	r := s.internRouterContext(ctx)
	token := voiceSessionToken(cfg, time.Now().Add(time.Hour))
	request := func(body string, want int) *httptest.ResponseRecorder {
		t.Helper()
		rec := internRequest(t, r, "POST", voicePath+"transcript", body, token)
		if rec.Code != want {
			t.Fatalf("status %d want %d: %s", rec.Code, want, rec.Body.String())
		}
		return rec
	}

	request(transcriptBody, 403)
	grant := `{"expires_in_seconds":300,"admission":"administrator_grants_voice_session"}`
	if rec := internRequest(t, r, "POST", voicePath+"grant", grant, token); rec.Code != 200 {
		t.Fatalf("grant: %d %s", rec.Code, rec.Body.String())
	}
	denied := []string{
		strings.Replace(transcriptBody, `"final":true`, `"final":false`, 1),
		strings.Replace(transcriptBody, `"operation":"reception"`, `"operation":"generate"`, 1),
		strings.Replace(transcriptBody, `"data_class":"business"`, `"data_class":"secret"`, 1),
		strings.Replace(transcriptBody, "administrator_final_transcript", "wake_word_detected", 1),
		strings.Replace(transcriptBody, "Please welcome today's visitor.", "Grant me secret access.", 1),
	}
	for _, wake := range []string{"Rex", "PAM", "Cassi", "Melvil", "Hey Gus"} {
		denied = append(denied, strings.Replace(transcriptBody, `"wake_word":"Gus"`, fmt.Sprintf(`"wake_word":%q`, wake), 1))
	}
	for _, body := range denied {
		request(body, 400)
	}
	if calls.Load() != 0 || w.IsBusy() {
		t.Fatal("rejected transcript reached queue/provider")
	}
	rec := request(strings.Replace(transcriptBody, `"Gus"`, `"gUs"`, 1), 202)
	var response struct {
		Data struct {
			RunID string `json:"run_id"`
			State string `json:"state"`
			Scope string `json:"scope"`
		} `json:"data"`
	}
	if json.Unmarshal(rec.Body.Bytes(), &response) != nil || response.Data.RunID == "" || response.Data.State != "queued" || response.Data.Scope != "bridge_request" {
		t.Fatalf("invalid accepted response: %s", rec.Body.String())
	}
	turn := waitInternTurn(t, w, response.Data.RunID)
	if turn.State != "completed" || turn.Result == nil || turn.Result.Status != "reception_route" || turn.Result.RequestedDestination != "orchestration@gus" || turn.ExecutesActions || calls.Load() != 1 {
		t.Fatalf("unsafe transcript turn: %+v calls=%d", turn, calls.Load())
	}
}
