package harness

import (
	"bytes"
	"crypto/ecdh"
	"crypto/ed25519"
	"encoding/json"
	"os"
	"testing"
)

func TestOriginalHarnessE2EEVectors(t *testing.T) {
	raw, err := os.ReadFile("testdata/original-e2ee-protocol.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture Frame
	if err = json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	object := func(key string) Frame { t.Helper(); return Frame(fixture[key].(map[string]any)) }
	binary := func(key string) []byte {
		t.Helper()
		b, e := decode(fixture[key], -1)
		if e != nil {
			t.Fatal(e)
		}
		return b
	}
	same := func(got, want any) {
		t.Helper()
		g, e := json.Marshal(got)
		if e != nil {
			t.Fatal(e)
		}
		w, e := json.Marshal(want)
		if e != nil {
			t.Fatal(e)
		}
		if !bytes.Equal(g, w) {
			t.Fatalf("protocol mismatch\ngot %s\nwant %s", g, w)
		}
	}
	identity := ed25519.NewKeyFromSeed(binary("deviceSeed"))
	crypto, err := newDeviceSessionCrypto(stringField(fixture, "machineId"), identity, binary("adapterPub"))
	if err != nil {
		t.Fatal(err)
	}
	crypto.eph, err = ecdh.X25519().NewPrivateKey(binary("deviceEphPriv"))
	if err != nil {
		t.Fatal(err)
	}
	same(crypto.hello(), object("hello"))
	if err = crypto.welcome(payloadOf(object("welcome"))); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(crypto.c2s, binary("c2s")) || !bytes.Equal(crypto.s2c, binary("s2c")) {
		t.Fatal("original session key mismatch")
	}
	// The original core encrypts only the payload; agent/machine fields are not a new full-frame wrapper.
	originalRequest := object("request")
	originalEnv := payloadOf(originalRequest)["__e2e"].(map[string]any)
	cipher, err := decode(originalEnv["ct"], -1)
	if err != nil {
		t.Fatal(err)
	}
	plain, err := openSealed(crypto.c2s, 0, []byte("1|autonomous_device_request||p|"), cipher)
	if err != nil {
		t.Fatal(err)
	}
	var originalPayload Frame
	if err = json.Unmarshal(plain, &originalPayload); err != nil {
		t.Fatal(err)
	}
	same(originalPayload, object("requestPayload"))
	outgoing, err := crypto.wrap(Frame{"type": "autonomous_device_request", "payload": object("requestPayload")})
	if err != nil {
		t.Fatal(err)
	}
	env := payloadOf(outgoing)["__e2e"].(Frame)
	cipher, err = decode(env["ct"], -1)
	if err != nil {
		t.Fatal(err)
	}
	plain, err = openSealed(binary("c2s"), 0, []byte("1|autonomous_device_request||p|"), cipher)
	if err != nil {
		t.Fatal(err)
	}
	var outgoingPayload Frame
	if err = json.Unmarshal(plain, &outgoingPayload); err != nil {
		t.Fatal(err)
	}
	same(outgoingPayload, object("requestPayload"))
	result, err := crypto.unwrap(object("result"))
	if err != nil {
		t.Fatal(err)
	}
	same(payloadOf(result), object("resultPayload"))
	if _, err = crypto.unwrap(object("result")); err == nil {
		t.Fatal("accepted repeated pairwise RPC reply")
	}
	event, err := crypto.unwrap(object("commander"))
	if err != nil {
		t.Fatal(err)
	}
	same(payloadOf(event), object("commanderPayload"))
	if err = crypto.rekey(payloadOf(object("rekey"))); err != nil {
		t.Fatal(err)
	}
	if err = crypto.rekey(payloadOf(object("rekey"))); err == nil {
		t.Fatal("accepted repeated rekey")
	}
	if _, err = crypto.unwrap(object("commander")); err == nil {
		t.Fatal("accepted obsolete group epoch")
	}
	event, err = crypto.unwrap(object("rotated"))
	if err != nil {
		t.Fatal(err)
	}
	same(payloadOf(event), object("commanderPayload"))
	pair := object("pair")
	pairID, err := decode(pair["pairId"], 16)
	if err != nil {
		t.Fatal(err)
	}
	ci := "autonomous-e2e-pair|agent:" + stringField(fixture, "machineId") + "|a:adapter|b:device"
	if ci != stringField(pair, "ci") || b64(cpaceGenerator(stringField(pair, "code"), pairID, ci).Encode(nil)) != stringField(pair, "generator") {
		t.Fatal("original device CPace domain mismatch")
	}
}

func TestSessionReplayAllowsBoundedReordering(t *testing.T) {
	var window sessionReplay
	for _, n := range []uint64{10, 8, 9} {
		if !window.allows(n) {
			t.Fatal("rejected in-window reordering")
		}
		window.commit(n)
	}
	if window.allows(8) {
		t.Fatal("accepted duplicate")
	}
	window.commit(5000)
	if window.allows(10) {
		t.Fatal("accepted stale counter")
	}
}
