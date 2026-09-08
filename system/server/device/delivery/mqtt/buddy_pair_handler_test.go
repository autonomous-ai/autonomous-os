package mqtthandler

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/eclipse/paho.golang/packets"
	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/buddy"
	"go.autonomous.ai/os/system/lib/mqtt"
	buddyhttp "go.autonomous.ai/os/system/server/buddy/delivery/http"
	"go.autonomous.ai/os/system/server/config"
)

// captureBuddyPairReply runs the real MQTT publisher against a minimal local
// broker, including the QoS 1 acknowledgement, without an external service.
func captureBuddyPairReply(t *testing.T, svc *buddy.Service) map[string]json.RawMessage {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	if err := listener.(*net.TCPListener).SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		t.Fatal(err)
	}
	type result struct {
		publish *packets.Publish
		err     error
	}
	replies := make(chan result, 1)
	done := make(chan struct{})
	go func() {
		defer close(done)
		conn, err := listener.Accept()
		if err != nil {
			replies <- result{err: err}
			return
		}
		defer conn.Close()
		if err := conn.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
			replies <- result{err: err}
			return
		}
		packet, err := packets.ReadPacket(conn)
		if err != nil {
			replies <- result{err: err}
			return
		}
		if _, ok := packet.Content.(*packets.Connect); !ok {
			replies <- result{err: fmt.Errorf("expected CONNECT, got %T", packet.Content)}
			return
		}
		if _, err := (&packets.Connack{Properties: &packets.Properties{}}).WriteTo(conn); err != nil {
			replies <- result{err: err}
			return
		}
		for {
			packet, err = packets.ReadPacket(conn)
			if err != nil {
				replies <- result{err: err}
				return
			}
			if _, ok := packet.Content.(*packets.Pingreq); !ok {
				break
			}
			if _, err := (&packets.Pingresp{}).WriteTo(conn); err != nil {
				replies <- result{err: err}
				return
			}
		}
		publish, ok := packet.Content.(*packets.Publish)
		if !ok {
			replies <- result{err: fmt.Errorf("expected PUBLISH, got %T", packet.Content)}
			return
		}
		if _, err := (&packets.Puback{PacketID: publish.PacketID}).WriteTo(conn); err != nil {
			replies <- result{err: err}
			return
		}
		replies <- result{publish: publish}
		// Keep the broker alive until the publisher closes its connection.
		_, _ = packets.ReadPacket(conn)
	}()
	t.Cleanup(func() { <-done })

	factory, err := mqtt.ProvideFactory(mqtt.Config{
		Endpoint: "127.0.0.1",
		Port:     listener.Addr().(*net.TCPAddr).Port,
	})
	if err != nil {
		t.Fatal(err)
	}
	h := DeviceMQTTHandler{
		config:       &config.Config{DeviceID: "buddy-pair-test", FDChannel: "test/fd"},
		mqttFactory:  factory,
		buddyService: svc,
	}
	if err := h.HandleMessage("test/fa", []byte(`{"cmd":"data","kind":"buddy.pair.start","data":{}}`)); err != nil {
		reply := <-replies
		t.Fatalf("HandleMessage: %v; broker: %v", err, reply.err)
	}
	reply := <-replies
	if reply.err != nil {
		t.Fatal(reply.err)
	}
	if reply.publish.Topic != "test/fd" || reply.publish.QoS != 1 {
		t.Fatalf("reply topic=%q QoS=%d", reply.publish.Topic, reply.publish.QoS)
	}
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(reply.publish.Payload, &envelope); err != nil {
		t.Fatal(err)
	}
	if string(envelope["type"]) != `"data"` || string(envelope["kind"]) != `"buddy.pair.start"` {
		t.Fatalf("unexpected envelope: %s", reply.publish.Payload)
	}
	return envelope
}

func TestBuddyPairStartMQTTConfirmHTTP(t *testing.T) {
	t.Chdir(t.TempDir())
	svc, err := buddy.ProvideService()
	if err != nil {
		t.Fatal(err)
	}
	envelope := captureBuddyPairReply(t, svc)
	if string(envelope["status"]) != `"success"` {
		t.Fatalf("unexpected status: %s", envelope["status"])
	}
	var data struct {
		Code      string `json:"code"`
		ExpiresIn int    `json:"expires_in"`
	}
	if err := json.Unmarshal(envelope["data"], &data); err != nil {
		t.Fatal(err)
	}
	if !regexp.MustCompile(`^[0-9]{6}$`).MatchString(data.Code) || data.ExpiresIn != 60 {
		t.Fatalf("unexpected pairing data: %+v", data)
	}

	h := buddyhttp.ProvideBuddyHandler(&config.Config{}, svc)
	router := gin.New()
	router.POST("/api/buddy/pair/confirm", h.PairConfirm)
	confirm := func() *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/api/buddy/pair/confirm", strings.NewReader(fmt.Sprintf(`{"code":%q,"name":"Test Mac"}`, data.Code)))
		request.Header.Set("Content-Type", "application/json")
		response := httptest.NewRecorder()
		router.ServeHTTP(response, request)
		return response
	}
	response := confirm()
	if response.Code != http.StatusOK {
		t.Fatalf("HTTP confirmation: %d %s", response.Code, response.Body.String())
	}
	var paired struct {
		Status int `json:"status"`
		Data   struct {
			Token string `json:"token"`
		} `json:"data"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &paired); err != nil {
		t.Fatal(err)
	}
	if paired.Status != 1 || paired.Data.Token == "" || svc.ValidateToken(paired.Data.Token) == nil {
		t.Fatal("HTTP confirmation did not issue a valid buddy token")
	}
	if response := confirm(); response.Code != http.StatusBadRequest {
		t.Fatalf("reused pairing code accepted: %d", response.Code)
	}
}

func TestBuddyPairStartMQTTUnavailable(t *testing.T) {
	envelope := captureBuddyPairReply(t, nil)
	if string(envelope["status"]) != `"failure"` || string(envelope["error"]) != `"buddy service unavailable"` {
		t.Fatalf("unexpected failure reply: %v", envelope)
	}
	if data := envelope["data"]; len(data) != 0 && string(data) != "null" {
		t.Fatalf("unavailable service returned pairing data: %s", data)
	}
}
