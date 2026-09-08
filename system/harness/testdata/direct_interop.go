// Run this optional local mDNS integration check against an installed Harness CLI checkout.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"syscall"
	"time"

	"go.autonomous.ai/os/system/harness"
)

func must(ok bool, msg string) {
	if !ok {
		panic(msg)
	}
}
func wait(label string, f func() bool) {
	deadline := time.Now().Add(25 * time.Second)
	for time.Now().Before(deadline) {
		if f() {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	panic("timeout " + label)
}
func main() {
	if len(os.Args) != 2 {
		panic("usage: go run ./system/harness/testdata/direct_interop.go /absolute/path/to/harness/cli")
	}
	cliRoot, e := filepath.Abs(os.Args[1])
	if e != nil {
		panic(e)
	}
	_, source, _, _ := runtime.Caller(0)

	cmd := exec.Command(filepath.Join(cliRoot, "node_modules/.bin/tsx"), filepath.Join(filepath.Dir(source), "direct-interop.mts"))
	cmd.Env = append(os.Environ(), "HARNESS_CLI_ROOT="+cliRoot)
	out, _ := cmd.StdoutPipe()
	cmd.Stderr = os.Stderr
	must(cmd.Start() == nil, "fixture start")
	defer func() {
		cmd.Process.Signal(syscall.SIGTERM)
		done := make(chan struct{})
		go func() { cmd.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			cmd.Process.Kill()
			<-done
		}
	}()
	sc := bufio.NewScanner(out)
	var ready map[string]any
	for sc.Scan() {
		if json.Unmarshal(sc.Bytes(), &ready) == nil && ready["port"] != nil {
			break
		}
	}
	must(ready["port"] != nil, "fixture port missing")
	go func() {
		for sc.Scan() {
		}
	}()
	base := fmt.Sprintf("http://127.0.0.1:%.0f", ready["port"])
	api := func(path string, body any) map[string]any {
		b, _ := json.Marshal(body)
		r, e := http.Post(base+path, "application/json", bytes.NewReader(b))
		if e != nil {
			panic(e)
		}
		defer r.Body.Close()
		raw, _ := io.ReadAll(r.Body)
		var f map[string]any
		json.Unmarshal(raw, &f)
		must(r.StatusCode == 200, "fixture error: "+string(raw))
		return f
	}
	dir, _ := os.MkdirTemp("", "os-direct-test-")
	defer os.RemoveAll(dir)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s, e := harness.NewService(dir, harness.Callbacks{})
	if e != nil {
		panic(e)
	}
	s.Start(ctx)
	server := httptest.NewUnstartedServer(s)
	listener, e := net.Listen("tcp", "0.0.0.0:0")
	if e != nil {
		panic(e)
	}
	server.Listener = listener
	server.Start()
	defer server.Close()
	port := listener.Addr().(*net.TCPAddr).Port
	ad := api("/advertise", map[string]any{"port": port})
	var deviceID string
	for i := 0; i < 3 && deviceID == ""; i++ {
		rows := api("/discover", nil)["devices"].([]any)
		for _, row := range rows {
			d := row.(map[string]any)
			if d["name"] == ad["name"] {
				deviceID = d["id"].(string)
				must(d["port"] == float64(port), "discovery ignored SRV port")
			}
		}
	}
	must(deviceID != "", "real mDNS discovery failed")
	info, e := s.StartPair(ctx)
	must(e == nil && len(info.Code) == 6, "OS generate code")
	wrong := "000000"
	if info.Code == wrong {
		wrong = "111111"
	}
	r := api("/pair", map[string]any{"id": deviceID, "code": wrong})
	must(r["error"] == "CODE_MISMATCH", "wrong code not reported: "+fmt.Sprint(r))
	wait("failed pairing cleanup", func() bool { return !s.PairStatus().Pairing })
	must(!s.Status().Paired, "wrong code wrote pin")
	info, e = s.StartPair(ctx)
	must(e == nil, "new window after mismatch")
	r = api("/pair", map[string]any{"id": deviceID, "code": info.Code})
	must(r["state"] == "paired", "pair failed: "+fmt.Sprint(r))
	fp := r["fingerprint"]
	wait("OS connected", func() bool { return s.Status().Connected })
	must(s.PairStatus().Code == "", "code persisted after completion")
	r, e = s.Request(ctx, harness.Frame{"type": "agents.list"})
	must(e == nil && r["machineId"] == "interop-machine", "encrypted list failed")
	req := harness.Frame{"type": "turn.send", "machineId": "interop-machine", "agentId": "agent-one", "text": "interop", "idempotencyKey": "direct-test"}
	r, e = s.Request(ctx, req)
	must(e == nil && r["status"] == "accepted", "send failed")
	r, e = s.Request(ctx, req)
	must(e == nil && r["status"] == "duplicate", "dedupe failed")
	state := api("/state", nil)
	must(state["submits"] == float64(1), "duplicate dispatched")
	must(state["backend"] == false && state["commander"] == true, "direct requires backend or recap presence absent")
	api("/restart", nil)
	wait("CLI restart reconnect", func() bool { return s.Status().Connected })
	time.Sleep(3500 * time.Millisecond)
	r, e = s.Request(ctx, harness.Frame{"type": "agents.list"})
	must(e == nil, "request after reconnect")
	cancel()
	server.Close()
	ctx2, cancel2 := context.WithCancel(context.Background())
	defer cancel2()
	s2, e := harness.NewService(dir, harness.Callbacks{})
	must(e == nil, "reload OS trust")
	s2.Start(ctx2)
	next := httptest.NewUnstartedServer(s2)
	listener2, e := net.Listen("tcp", ":"+strconv.Itoa(port))
	must(e == nil, "restart listener")
	next.Listener = listener2
	next.Start()
	defer next.Close()
	api("/restart", nil)
	wait("OS restart reconnect", func() bool { return s2.Status().Connected })
	r, e = s2.Request(ctx2, harness.Frame{"type": "agents.list"})
	must(e == nil, "list after OS restart")
	api("/revoke", map[string]any{"fingerprint": fp})
	wait("revoke disconnect", func() bool { return !s2.Status().Connected })
	_, e = s2.Request(ctx2, harness.Frame{"type": "agents.list"})
	must(e != nil, "revoked access remained")
	must(s2.Unpair() == nil && !s2.Status().Paired, "OS unpair")
	fmt.Println("PASS real mDNS + actual BackendSocket/E2eeManager + direct client + Go Service: wrong-code failure/retry, pair, encrypted list/send/dedupe, CLI and OS restart reconnect, revoke/unpair. Backend never connected.")
}
