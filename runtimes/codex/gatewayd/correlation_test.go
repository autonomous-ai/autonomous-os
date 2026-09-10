package gatewayd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestGatewaySteersConcurrentMessagesIntoActiveTurn(t *testing.T) {
	dir := t.TempDir()
	binary := writeFakeCodex(t, dir, filepath.Join(dir, "argv.log"))
	url, _ := startServer(t, binary, dir)
	conn := dial(t, url, testToken)
	_ = readFrame(t, conn) // ready
	for i := 1; i <= 3; i++ {
		if err := conn.WriteJSON(map[string]any{"type": "message.send", "id": fmt.Sprintf("req-%d", i), "run_id": fmt.Sprintf("run-%d", i), "payload": map[string]any{"content": fmt.Sprintf("followup-%d", i)}}); err != nil {
			t.Fatal(err)
		}
	}
	frames := readTurnFrames(t, conn)
	if len(frames) < 3 {
		t.Fatalf("active turn missing events: %v", frames)
	}
	for _, frame := range frames {
		if frame["request_id"] != "req-1" || frame["run_id"] != "run-1" {
			t.Fatalf("steered turn must retain first correlation: %v", frame)
		}
	}
	data, err := os.ReadFile(filepath.Join(dir, "argv.log"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(string(data), "METHOD:turn/steer") != 2 {
		t.Fatalf("want two turn/steer requests, log: %s", data)
	}
}

func TestGatewayInterruptsTimedOutAppServerTurn(t *testing.T) {
	dir := t.TempDir()
	binary := writeFakeCodex(t, dir, filepath.Join(dir, "argv.log"))
	url, _ := startServerTimeout(t, binary, dir, 25*time.Millisecond)
	conn := dial(t, url, testToken)
	_ = readFrame(t, conn) // ready
	sendMessage(t, conn, "slow turn")
	frames := readTurnFrames(t, conn)
	last := frames[len(frames)-1]
	if last["type"] != "bridge.error" || last["error"] != "timeout" {
		t.Fatalf("want timeout bridge error, got %v", last)
	}
	data, err := os.ReadFile(filepath.Join(dir, "argv.log"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), "METHOD:turn/interrupt") {
		t.Fatalf("timeout did not interrupt active App Server turn: %s", data)
	}
}

func TestCorrelationNeverTagsControlOrRejectionFrames(t *testing.T) {
	for _, kind := range []string{"pong", "bridge.status", "bridge.rejected"} {
		raw, _ := json.Marshal(map[string]any{"type": kind, "request_id": "other", "run_id": "run-other"})
		var result map[string]any
		_ = json.Unmarshal(correlateTurnFrame(raw, "active", "run-active"), &result)
		if result["request_id"] != "other" || result["run_id"] != "run-other" {
			t.Fatalf("control frame mislabeled: %v", result)
		}
	}
}
