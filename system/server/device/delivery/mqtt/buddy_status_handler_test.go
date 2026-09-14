package mqtthandler

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/eclipse/paho.golang/packets"
	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/buddy"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/mqtt"
	buddyhttp "go.autonomous.ai/os/system/server/buddy/delivery/http"
	"go.autonomous.ai/os/system/server/config"
)

func TestBuddyStatusMQTTQuery(t *testing.T) {
	t.Chdir(t.TempDir())
	svc, err := buddy.ProvideService()
	if err != nil {
		t.Fatal(err)
	}
	for _, paired := range []bool{false, true} {
		if paired {
			code, _ := svc.IssuePairingCode()
			if _, err := svc.ConfirmPairing("Test Mac", "private fingerprint", "macOS", code); err != nil {
				t.Fatal(err)
			}
		}
		reply := captureBuddyPairReply(t, svc, domain.KindBuddyStatus)
		if string(reply["status"]) != `"success"` {
			t.Fatalf("query failed: %v", reply)
		}
		var status buddy.Status
		if err := json.Unmarshal(reply["data"], &status); err != nil {
			t.Fatal(err)
		}
		if status.Paired != paired || status.Connected || status.InstanceID == "" {
			t.Fatalf("bad status: %+v", status)
		}
		for _, secret := range []string{`"token"`, `"code"`, `"fingerprint"`} {
			if strings.Contains(string(reply["data"]), secret) {
				t.Fatalf("status exposes %s", secret)
			}
		}
	}
	reply := captureBuddyPairReply(t, nil, domain.KindBuddyStatus)
	if string(reply["status"]) != `"failure"` || string(reply["error"]) != `"buddy service unavailable"` {
		t.Fatalf("unexpected unavailable: %v", reply)
	}
}

// statusBroker captures unsolicited QoS-1 publications across successive clients.
func statusBroker(t *testing.T) (*mqtt.Factory, <-chan []byte) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	messages := make(chan []byte, 16)
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			func() {
				defer conn.Close()
				_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
				packet, err := packets.ReadPacket(conn)
				if err != nil {
					return
				}
				if _, ok := packet.Content.(*packets.Connect); !ok {
					t.Error("expected CONNECT")
					return
				}
				if _, err := (&packets.Connack{Properties: &packets.Properties{}}).WriteTo(conn); err != nil {
					return
				}
				for {
					packet, err := packets.ReadPacket(conn)
					if err != nil {
						return
					}
					switch p := packet.Content.(type) {
					case *packets.Pingreq:
						if _, err := (&packets.Pingresp{}).WriteTo(conn); err != nil {
							return
						}
					case *packets.Publish:
						if p.Topic != "test/fd" || p.QoS != 1 || p.Retain {
							t.Errorf("invalid delivery: %+v", p)
						}
						messages <- append([]byte(nil), p.Payload...)
						if _, err := (&packets.Puback{PacketID: p.PacketID}).WriteTo(conn); err != nil {
							return
						}
					case *packets.Disconnect:
						return
					}
				}
			}()
		}
	}()
	t.Cleanup(func() { listener.Close(); <-done })
	factory, err := mqtt.ProvideFactory(mqtt.Config{Endpoint: "127.0.0.1", Port: listener.Addr().(*net.TCPAddr).Port})
	if err != nil {
		t.Fatal(err)
	}
	return factory, messages
}

func TestBuddyStatusMQTTEventsFromHTTP(t *testing.T) {
	t.Chdir(t.TempDir())
	svc, err := buddy.ProvideService()
	if err != nil {
		t.Fatal(err)
	}
	factory, messages := statusBroker(t)
	h := DeviceMQTTHandler{config: &config.Config{DeviceID: "status-test", FDChannel: "test/fd"}, mqttFactory: factory, buddyService: svc}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); h.StartBuddyStatusLoop(ctx) }()
	t.Cleanup(func() { cancel(); <-done })
	readStatus := func() buddy.Status {
		t.Helper()
		select {
		case payload := <-messages:
			var response struct {
				Type   string       `json:"type"`
				Kind   string       `json:"kind"`
				Status string       `json:"status"`
				Data   buddy.Status `json:"data"`
			}
			if err := json.Unmarshal(payload, &response); err != nil {
				t.Fatal(err)
			}
			if response.Type != "data" || response.Kind != "buddy.status" || response.Status != "success" {
				t.Fatalf("bad event: %s", payload)
			}
			return response.Data
		case <-time.After(5 * time.Second):
			t.Fatal("missing unsolicited status")
		}
		return buddy.Status{}
	}
	initial := readStatus()
	if initial.Paired || initial.Revision != 0 {
		t.Fatalf("bad initial: %+v", initial)
	}
	httpHandler := buddyhttp.ProvideBuddyHandler(&config.Config{}, svc)
	router := gin.New()
	router.POST("/pair/confirm", httpHandler.PairConfirm)
	router.DELETE("/buddy", httpHandler.Revoke)
	code, _ := svc.IssuePairingCode()
	request := httptest.NewRequest(http.MethodPost, "/pair/confirm", strings.NewReader(fmt.Sprintf(`{"code":%q,"name":"Test Mac"}`, code)))
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	router.ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("confirm failed: %s", response.Body.String())
	}
	paired := readStatus()
	if !paired.Paired || paired.Connected || paired.Name != "Test Mac" || paired.Revision <= initial.Revision || paired.InstanceID != initial.InstanceID {
		t.Fatalf("bad paired status: %+v", paired)
	}
	response = httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodDelete, "/buddy", nil))
	if response.Code != http.StatusOK {
		t.Fatalf("revoke failed: %s", response.Body.String())
	}
	revoked := readStatus()
	if revoked.Paired || revoked.Connected || revoked.BuddyID != "" || revoked.Revision <= paired.Revision {
		t.Fatalf("bad revoked status: %+v", revoked)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("status worker did not stop")
	}
}

func TestBuddyStatusCancelledBeforeConnect(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	h := DeviceMQTTHandler{buddyService: &buddy.Service{}}
	// Cancelled startup must exit before using unconfigured networking.
	h.StartBuddyStatusLoop(ctx)
}
