package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"gopkg.in/natefinch/lumberjack.v2"
)

// compactPreview returns the first max bytes of data, replacing control
// characters and the trailing newline with spaces so the snippet renders
// cleanly inside a journal log line.
func compactPreview(data []byte, max int) string {
	if len(data) > max {
		data = data[:max]
	}
	out := make([]byte, len(data))
	for i, b := range data {
		if b < 0x20 || b == 0x7f {
			out[i] = ' '
		} else {
			out[i] = b
		}
	}
	return string(out)
}

// Config is loaded from buddy.json.
type Config struct {
	Enabled            bool   `json:"enabled"`
	DeviceName         string `json:"device_name"`
	HTTPPort           int    `json:"http_port"`
	LeLampURL          string `json:"lelamp_url"`
	LampURL            string `json:"lamp_url"`
	ApprovalTimeoutSec int    `json:"approval_timeout_sec"`
	// NarrationLang picks the language used by the Narrator (UC-9
	// activity status announcements). Supported values live in
	// narrationStrings (i18n.go); unsupported values fall back to
	// English at runtime via supportedLang().
	NarrationLang string `json:"narration_lang"`
}

func main() {
	configPath := flag.String("config", "/root/config/buddy.json", "path to config file")
	logPath := flag.String("log", "/var/log/claude-desktop-buddy.log", "path to log file")
	flag.Parse()

	// Rotating log file: 2 MB per file, keep 10 backups (same as lamp)
	rotatingWriter := &lumberjack.Logger{
		Filename:   *logPath,
		MaxSize:    2, // MB
		MaxBackups: 10,
		MaxAge:     0,
		Compress:   false,
	}
	defer rotatingWriter.Close()
	log.SetOutput(io.MultiWriter(os.Stdout, rotatingWriter))
	log.SetFlags(log.Ldate | log.Ltime)

	cfg := loadConfig(*configPath)
	if !cfg.Enabled {
		log.Println("[buddy] disabled in config, exiting")
		return
	}

	cfg.DeviceName = resolveDeviceName(cfg.DeviceName, cfg.LampURL)

	// Register a BlueZ agent so LE Secure Connections pairing can complete.
	// Without an agent, BlueZ rejects pairing requests and Claude Desktop's
	// Hardware Buddy picker won't see (or won't connect to) the device.
	if err := registerBluezAgent(); err != nil {
		log.Printf("[buddy] WARN: register agent failed: %v (pairing will likely fail)", err)
	}

	bridge := NewBridge(cfg.LeLampURL, cfg.LampURL)
	startTime := time.Now()

	// Narrator (UC-9): short TTS announcements on state changes and
	// per-tool-use blocks. Shares the LeLamp TTS endpoint with the rest
	// of the voice pipeline so LeLamp's own mute / music-busy logic
	// applies.
	narrator := NewNarrator(cfg.NarrationLang, bridge.speakTTS)
	// Warm the TTS cache once LeLamp has had a chance to come up.
	// Fire-and-forget; prerender requests are queued by LeLamp and any
	// 503 / 409 responses are ignored so we don't block startup.
	go func() {
		time.Sleep(8 * time.Second)
		narrator.Warmup(bridge.prerenderTTS)
		log.Println("[narrator] prerender warmup dispatched")
	}()

	// State machine with bridge callback. We wrap bridge.OnStateChange
	// so narration triggers fire alongside LED/display reactions
	// without making bridge.go aware of the narrator.
	// Restore lifetime approval / denial counters so /status reports
	// the right numbers right after a restart.
	persisted := LoadStats()

	sm := NewStateMachine(func(old, next BuddyState, hb *Heartbeat) {
		bridge.OnStateChange(old, next, hb)
		switch {
		case old == StateSleep && next != StateSleep:
			// Fresh BLE session — announce connect and reset turn dedupe
			// so the first activity gets full narration.
			narrator.StartTurn()
			narrator.Say(NarrateConnected)
		case old != StateSleep && next == StateSleep:
			narrator.Say(NarrateDisconnected)
		case old != StateBusy && next == StateBusy:
			// Fresh activity window — reset per-turn dedupe so tool /
			// thinking narrations can fire again, then announce.
			narrator.StartTurn()
			narrator.Say(NarrateBusyStart)
		case old == StateBusy && next == StateIdle:
			narrator.Say(NarrateDone)
			// Done = quick celebratory emotion. LeLamp coordinates the
			// servo + LED together via /emotion so the lamp visibly
			// "exhales" between turns.
			bridge.expressEmotion("happy", 0.7)
		}
	})
	sm.SeedStats(persisted.Approved, persisted.Denied)

	// BLE server — assign to package-level `ble` so the onMessage closure
	// captures the same variable the closure body dereferences. Using `:=`
	// here would shadow the package var and leave the closure seeing nil.
	ble = NewBLEServer(cfg.DeviceName, func(data []byte) {
		handleBLEMessage(data, sm, ble, bridge, narrator, cfg.DeviceName, startTime)
	}, func(connected bool) {
		sm.SetConnected(connected)
		if !connected {
			xfer.Abort()
		}
	})

	// Transient state expiry ticker
	go func() {
		ticker := time.NewTicker(500 * time.Millisecond)
		defer ticker.Stop()
		for range ticker.C {
			sm.CheckTransientExpiry()
		}
	}()

	// HTTP server for OpenClaw skill
	httpSrv := NewHTTPServer(cfg.HTTPPort, sm, ble)
	go func() {
		if err := httpSrv.Start(); err != nil {
			log.Fatalf("[buddy] http server error: %v", err)
		}
	}()

	// Start BLE (blocking — advertising loop)
	log.Printf("[buddy] starting Claude Desktop Buddy plugin (%s)", cfg.DeviceName)
	log.Printf("[buddy] LeLamp: %s, Lamp: %s, HTTP: :%d", cfg.LeLampURL, cfg.LampURL, cfg.HTTPPort)

	if err := ble.Start(); err != nil {
		log.Fatalf("[buddy] BLE start error: %v", err)
	}

	// Mark as connected once BLE is advertising
	// Actual connection detection happens via heartbeat receipt
	log.Println("[buddy] BLE advertising started, waiting for Claude Desktop connection...")

	// Keep main goroutine alive
	select {}
}

// ble is declared as package var so handleBLEMessage can reference it via closure
var ble *BLEServer

// xfer holds the single active folder-push transfer from Claude Desktop.
var xfer Transfer

func handleBLEMessage(data []byte, sm *StateMachine, bleSrv *BLEServer, bridge *Bridge, narrator *Narrator, deviceName string, startTime time.Time) {
	msg, lost, err := ParseOrSalvage(data)
	if err != nil {
		// BLE write-without-response has no ACK, so BlueZ silently drops
		// packets under load. Three failure modes show up here, all
		// unrecoverable, all worth tagging so the journal hints at why:
		//   - prefix-lost: the line doesn't start with '{', so the original
		//     payload's head is gone. Salvage already tried known openers
		//     and failed.
		//   - truncated: starts with '{' but doesn't end with '}'. Tail of
		//     the line dropped, brackets never closed.
		//   - mid-corruption: brackets line up but a chunk inside an
		//     `entries` / `content` array got dropped, so unmarshal
		//     trips on a stray character mid-payload.
		// Abort any in-progress char transfer because we lost framing.
		preview := compactPreview(data, 80)
		category := "mid-corruption"
		switch {
		case len(data) == 0 || data[0] != '{':
			category = "prefix-lost"
			xfer.Abort()
		case !bytes.HasSuffix(bytes.TrimRight(data, "\n"), []byte("}")):
			category = "truncated"
		}
		log.Printf("[ble] dropped %d-byte BLE message (%s): %v — %q", len(data), category, err, preview)
		return
	}
	if lost > 0 {
		// Claude Desktop writes BLE chunks via Write-Without-Response, which
		// has no ATT_CONFIRM, so BlueZ silently drops packets under load. When
		// that happens we salvage the tail of the line. The dropped bytes are
		// gone — affected file transfers will be incomplete but the session
		// stays alive for the remaining chunks.
		log.Printf("[ble] WARN: dropped %d corrupted prefix bytes (BLE packet loss)", lost)
		xfer.Abort()
	}

	switch m := msg.(type) {
	case *Heartbeat:
		// First heartbeat means Desktop is connected
		if !sm.Connected() {
			sm.SetConnected(true)
			log.Println("[ble] Claude Desktop connected")
		}
		// Claude Desktop pings ~every second while a task runs; logging
		// each one floods the journal. Only emit when something the
		// operator actually cares about changed (running count, msg
		// text, waiting count, or prompt arrival/clear). Token counts
		// drift on every ping and are intentionally excluded.
		if prev := sm.LastHeartbeat(); heartbeatChanged(prev, m) {
			log.Printf("[ble] heartbeat total=%d running=%d waiting=%d tokens=%d today=%d msg=%q entries=%d prompt=%v",
				m.Total, m.Running, m.Waiting, m.Tokens, m.TokensToday, m.Msg, len(m.Entries), m.Prompt != nil)
		}
		sm.HandleHeartbeat(m)

	case *TimeSync:
		log.Printf("[ble] time sync: epoch=%d, offset=%d", m.Time[0], m.Time[1])
		// Ack not required for time sync (no cmd field)

	case *Event:
		// Stream of chat turns and other events from Claude Desktop.
		// Log the full content (no truncation) so downstream consumers
		// reading the journal — and us during integration work — see
		// everything Claude Desktop sent.
		log.Printf("[ble] event evt=%q role=%q content=%q", m.Evt, m.Role, m.TurnText())
		// Fan out to Lamp so use cases (TTS, display, etc.) can subscribe.
		bridge.OnEvent(m)
		// UC-9 narration: a new user turn resets per-turn throttle;
		// assistant turns are inspected block-by-block so tool_use and
		// thinking blocks become short TTS announcements.
		if m.Evt == "turn" {
			switch m.Role {
			case "user":
				narrator.StartTurn()
			case "assistant":
				for _, b := range m.Blocks() {
					switch b.Type {
					case "thinking":
						narrator.Say(NarrateThinking)
					case "tool_use":
						narrator.SayTool(b.Name)
					}
				}
			}
		}
		// No ack required (no cmd field).

	case *Command:
		log.Printf("[ble] command: %s", m.Cmd)
		switch m.Cmd {
		case "status":
			approved, denied := sm.ApprovalStats()
			resp := MakeStatusAck(deviceName, time.Since(startTime), approved, denied)
			if err := bleSrv.Send(resp); err != nil {
				log.Printf("[ble] send status ack error: %v", err)
			}
		case "owner":
			log.Printf("[ble] owner set to: %s", m.Name)
			if err := bleSrv.Send(MakeAck("owner", true)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		case "name":
			log.Printf("[ble] name set to: %s", m.Name)
			if err := bleSrv.Send(MakeAck("name", true)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		case "unpair":
			log.Println("[ble] unpair requested")
			xfer.Abort()
			if err := bleSrv.Send(MakeAck("unpair", true)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
			sm.SetConnected(false)

		// Folder-push streaming protocol — persist to disk under CharsRoot.
		case "char_begin":
			ok := true
			if err := xfer.Begin(m.Name, m.Total); err != nil {
				log.Printf("[xfer] begin error: %v", err)
				ok = false
			}
			if err := bleSrv.Send(MakeAck("char_begin", ok)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		case "file":
			ok := true
			if err := xfer.StartFile(m.Path, m.Size); err != nil {
				log.Printf("[xfer] file error: %v", err)
				ok = false
			}
			if err := bleSrv.Send(MakeAck("file", ok)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		case "chunk":
			n, err := xfer.WriteChunk(m.D)
			if err != nil {
				log.Printf("[xfer] chunk error: %v", err)
				if err := bleSrv.Send(MakeAckN("chunk", false, n)); err != nil {
					log.Printf("[ble] send ack error: %v", err)
				}
				break
			}
			if err := bleSrv.Send(MakeAckN("chunk", true, n)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		case "file_end":
			n, err := xfer.EndFile()
			ok := err == nil
			if err != nil {
				log.Printf("[xfer] file_end error: %v", err)
			}
			if err := bleSrv.Send(MakeAckN("file_end", ok, n)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		case "char_end":
			xfer.End()
			if err := bleSrv.Send(MakeAck("char_end", true)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}

		default:
			log.Printf("[ble] unknown command: %s", m.Cmd)
			if err := bleSrv.Send(MakeAck(m.Cmd, false)); err != nil {
				log.Printf("[ble] send ack error: %v", err)
			}
		}
	}
}

func loadConfig(path string) Config {
	cfg := Config{
		Enabled:            true,
		DeviceName:         "Claude-lamp-{MAC}",
		HTTPPort:           5002,
		LeLampURL:          "http://127.0.0.1:5001",
		LampURL:            "http://127.0.0.1:5000",
		ApprovalTimeoutSec: 30,
		NarrationLang:      "vi",
	}

	data, err := os.ReadFile(path)
	if err != nil {
		log.Printf("[buddy] config %s not found, using defaults", path)
		return cfg
	}

	if err := json.Unmarshal(data, &cfg); err != nil {
		log.Printf("[buddy] config parse error: %v, using defaults", err)
		return cfg
	}

	log.Printf("[buddy] loaded config from %s", path)
	return cfg
}

// resolveDeviceName expands the {MAC} placeholder in name by fetching the
// hardware MAC suffix from Lamp's /api/system/network. Buddy may start before
// Lamp is ready, so we retry transport errors for a short window. Names
// without the placeholder pass through untouched.
//
// MAC suffix is preferred over device_id because it's hardware-derived
// (last 4 chars of Pi serial, or eth0 MAC on non-Pi boards) and available
// before /device/setup runs, whereas DeviceID is empty pre-provisioning.
// Matching the mDNS hostname (`lamp-xxxx.local`) also makes the BLE name
// recognisable to users who already know their device by its .local name.
//
// The suffix is truncated to 4 chars so the resolved name fits in the
// 31-byte primary BLE advertisement alongside the 128-bit Nordic UART
// service UUID. With a long name the system pushes it to the scan
// response, which some scanners only fetch via active scan and may miss.
func resolveDeviceName(name, lampURL string) string {
	if name == "" {
		name = "Claude-lamp-{MAC}"
	}
	if !strings.Contains(name, "{MAC}") {
		return name
	}

	mac, reason := fetchMAC(lampURL)
	switch {
	case mac != "":
		log.Printf("[buddy] resolved mac=%q from Lamp", mac)
	case reason == "empty":
		log.Printf("[buddy] WARN: Lamp reachable at %s but mac is empty — hardware serial/MAC unreadable", lampURL)
		mac = "unk"
	default:
		log.Printf("[buddy] WARN: failed to fetch mac from %s after %d attempts (%s)",
			lampURL, fetchAttempts, reason)
		mac = "unk"
	}
	short := shortMAC(mac)
	if short != mac {
		log.Printf("[buddy] shortened mac %q → %q for BLE adv fit", mac, short)
	}
	return strings.ReplaceAll(name, "{MAC}", short)
}

// shortMAC returns a compact lowercase form of the MAC suitable for the
// BLE local name. Takes the last dash-separated segment (e.g. "Lamp-A1B2"
// → "a1b2") and truncates to 4 chars. Lowercasing matches the mDNS
// hostname convention (`lamp-xxxx.local`).
func shortMAC(mac string) string {
	if mac == "" {
		return "unk"
	}
	if i := strings.LastIndexByte(mac, '-'); i >= 0 && i+1 < len(mac) {
		mac = mac[i+1:]
	}
	if len(mac) > 4 {
		mac = mac[len(mac)-4:]
	}
	return strings.ToLower(mac)
}

const fetchAttempts = 15

// fetchMAC returns (mac, reason). reason is one of:
//
//	"" on success, "empty" if Lamp answered with an empty mac (hardware
//	serial/MAC unreadable), or a transport-level failure summary if all
//	retries failed.
func fetchMAC(lampURL string) (string, string) {
	client := &http.Client{Timeout: 3 * time.Second}
	url := lampURL + "/api/system/network"
	var lastErr string
	for i := 0; i < fetchAttempts; i++ {
		mac, ok, errStr := tryFetchMAC(client, url)
		if ok {
			if mac == "" {
				return "", "empty"
			}
			return mac, ""
		}
		lastErr = errStr
		time.Sleep(2 * time.Second)
	}
	if lastErr == "" {
		lastErr = "unknown error"
	}
	return "", lastErr
}

// tryFetchMAC returns (mac, ok, errStr). ok=true means Lamp answered with
// a parseable response (mac may still be empty if hardware ID is unset).
// ok=false means transport/decode failure — caller should retry.
func tryFetchMAC(client *http.Client, url string) (string, bool, string) {
	resp, err := client.Get(url)
	if err != nil {
		return "", false, "http: " + err.Error()
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", false, "http " + resp.Status
	}
	var wrap struct {
		Data struct {
			MAC string `json:"mac"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&wrap); err != nil {
		return "", false, "decode: " + err.Error()
	}
	return wrap.Data.MAC, true, ""
}

// heartbeatChanged reports whether enough fields differ between the
// previous and current Heartbeat to warrant a new journal entry. We
// deliberately ignore Tokens / TokensToday because they tick on every
// ping; the operator cares about running count, status text, waiting
// queue, and whether a permission prompt arrived or cleared.
func heartbeatChanged(prev, curr *Heartbeat) bool {
	if prev == nil {
		return true
	}
	if prev.Running != curr.Running ||
		prev.Waiting != curr.Waiting ||
		prev.Msg != curr.Msg ||
		(prev.Prompt == nil) != (curr.Prompt == nil) {
		return true
	}
	if prev.Prompt != nil && curr.Prompt != nil && prev.Prompt.ID != curr.Prompt.ID {
		return true
	}
	return false
}
