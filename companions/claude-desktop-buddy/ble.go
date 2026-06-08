package main

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"sync"

	"tinygo.org/x/bluetooth"
)

var adapter = bluetooth.DefaultAdapter

// Nordic UART Service UUIDs
var (
	nusServiceUUID = bluetooth.NewUUID([16]byte{
		0x6e, 0x40, 0x00, 0x01, 0xb5, 0xa3, 0xf3, 0x93,
		0xe0, 0xa9, 0xe5, 0x0e, 0x24, 0xdc, 0xca, 0x9e,
	})
	nusRXUUID = bluetooth.NewUUID([16]byte{
		0x6e, 0x40, 0x00, 0x02, 0xb5, 0xa3, 0xf3, 0x93,
		0xe0, 0xa9, 0xe5, 0x0e, 0x24, 0xdc, 0xca, 0x9e,
	})
	nusTXUUID = bluetooth.NewUUID([16]byte{
		0x6e, 0x40, 0x00, 0x03, 0xb5, 0xa3, 0xf3, 0x93,
		0xe0, 0xa9, 0xe5, 0x0e, 0x24, 0xdc, 0xca, 0x9e,
	})
)

// BLEServer manages the Nordic UART BLE GATT server.
//
// BlueZ dispatches D-Bus methods on separate goroutines — WriteEvent and
// SetConnectHandler callbacks can fire concurrently — and ble.Send is called
// from the HTTP server too. Serialization:
//   - rxMu   guards rxBuf accumulation / line extraction.
//   - sendMu guards txChar writes.
//   - A single processor goroutine drains msgCh and evtCh, calling onMessage
//     and onConnect sequentially so the transfer state they touch is race-free.
type BLEServer struct {
	rxMu       sync.Mutex // also guards `connected` (shared with the D-Bus connect-handler dispatch)
	sendMu     sync.Mutex
	deviceName string
	txChar     bluetooth.Characteristic
	onMessage  func([]byte)
	onConnect  func(connected bool)
	rxBuf      bytes.Buffer
	connected  bool
	msgCh      chan []byte
	evtCh      chan bool
}

func NewBLEServer(deviceName string, onMessage func([]byte), onConnect func(connected bool)) *BLEServer {
	s := &BLEServer{
		deviceName: deviceName,
		onMessage:  onMessage,
		onConnect:  onConnect,
		msgCh:      make(chan []byte, 64),
		evtCh:      make(chan bool, 4),
	}
	go s.processor()
	return s
}

// processor serializes user callbacks. Running in one goroutine means
// onMessage and onConnect never race on the shared transfer state.
func (s *BLEServer) processor() {
	for {
		select {
		case line := <-s.msgCh:
			if s.onMessage != nil {
				s.onMessage(line)
			}
		case connected := <-s.evtCh:
			if s.onConnect != nil {
				s.onConnect(connected)
			}
		}
	}
}

// Start initializes the BLE adapter and begins advertising.
func (s *BLEServer) Start() error {
	log.Println("[ble] enabling adapter...")
	if err := adapter.Enable(); err != nil {
		return err
	}

	// tinygo bluetooth v0.14.0 has a TODO for MinInterval/MaxInterval on Linux,
	// so BlueZ falls back to its 1.28s default — way too slow for macOS scan
	// windows, leaving Claude Desktop frequently unable to discover the device.
	// Override via the kernel debugfs knobs (writable as root) before
	// adv.Start() so the registered advertisement uses faster timing.
	tuneAdvIntervals()

	// Reset the RX buffer on disconnect so a leftover partial line from the
	// prior session doesn't corrupt the next. Dedup both edges — BlueZ can
	// fire the handler redundantly (e.g. when the remote briefly re-subscribes)
	// and each onConnect call has side effects (state transitions, xfer reset),
	// so we only forward genuine edge changes to the processor goroutine.
	adapter.SetConnectHandler(func(device bluetooth.Device, connected bool) {
		s.rxMu.Lock()
		changed := s.connected != connected
		s.connected = connected
		if !connected {
			s.rxBuf.Reset()
		}
		s.rxMu.Unlock()

		if !changed {
			return
		}
		if connected {
			log.Println("[ble] device connected")
		} else {
			log.Println("[ble] device disconnected")
		}
		s.evtCh <- connected
	})

	// Hardware Buddy spec recommends LE Secure Connections bonding, but
	// Claude Desktop's current Mac client connects without auto-triggering
	// SMP, leaving encrypted-only chrs inaccessible ("No response" in the
	// panel). For now drop the secure-* flags so the chrs are reachable
	// over the unencrypted link; bonding can be re-introduced once we
	// figure out how to make Claude Desktop initiate the pairing handshake.
	err := adapter.AddService(&bluetooth.Service{
		UUID: nusServiceUUID,
		Characteristics: []bluetooth.CharacteristicConfig{
			{
				UUID: nusRXUUID, // Desktop writes here (Desktop → Device)
				Flags: bluetooth.CharacteristicWritePermission |
					bluetooth.CharacteristicWriteWithoutResponsePermission,
				WriteEvent: func(client bluetooth.Connection, offset int, value []byte) {
					s.handleRX(value)
				},
			},
			{
				Handle: &s.txChar,
				UUID:   nusTXUUID, // Device writes here (Device → Desktop)
				Flags: bluetooth.CharacteristicNotifyPermission |
					bluetooth.CharacteristicReadPermission,
			},
		},
	})
	if err != nil {
		return err
	}

	log.Printf("[ble] advertising as %q...", s.deviceName)
	adv := adapter.DefaultAdvertisement()
	err = adv.Configure(bluetooth.AdvertisementOptions{
		LocalName:    s.deviceName,
		ServiceUUIDs: []bluetooth.UUID{nusServiceUUID},
	})
	if err != nil {
		return err
	}

	log.Println("[ble] calling adv.Start()...")
	if err := adv.Start(); err != nil {
		log.Printf("[ble] adv.Start() failed: %v", err)
	} else {
		log.Println("[ble] adv.Start() succeeded")
	}

	log.Println("[ble] BLE advertising started")
	return nil
}

// handleRX accumulates incoming bytes and forwards each complete
// newline-terminated line to the processor goroutine. Partial data stays in
// rxBuf until the terminating '\n' arrives in a later write. rxMu keeps
// concurrent WriteValue dispatches from racing on the buffer.
func (s *BLEServer) handleRX(data []byte) {
	s.rxMu.Lock()
	s.rxBuf.Write(data)

	var lines [][]byte
	for {
		buf := s.rxBuf.Bytes()
		idx := bytes.IndexByte(buf, '\n')
		if idx < 0 {
			break
		}
		if idx > 0 {
			line := make([]byte, idx)
			copy(line, buf[:idx])
			lines = append(lines, line)
		}
		s.rxBuf.Next(idx + 1)
	}
	s.rxMu.Unlock()

	for _, line := range lines {
		s.msgCh <- line
	}
}

// Send writes a JSON line to the TX characteristic (Device → Desktop).
// We chunk at 180 bytes — safely under the macOS-negotiated MTU (~185) so a
// typical ack fits in one notification and Claude Desktop never has to
// reassemble a fragmented reply. sendMu serializes writes across the
// processor goroutine and HTTP approval handlers.
func (s *BLEServer) Send(data []byte) error {
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	const maxNotify = 180
	for len(data) > 0 {
		chunk := data
		if len(chunk) > maxNotify {
			chunk = data[:maxNotify]
		}
		if _, err := s.txChar.Write(chunk); err != nil {
			return err
		}
		data = data[len(chunk):]
	}
	return nil
}

// ensureKernelAdv uses btmgmt to create a kernel-level BLE advertisement.
// On Pi5, tinygo's D-Bus RegisterAdvertisement may not create one, causing
// the device to be invisible to BLE scanners despite the GATT service being
// registered. This is a workaround.
// Close is a no-op for tinygo bluetooth.
func (s *BLEServer) Close() {}

// tuneAdvIntervals writes desired LE advertising min/max intervals to the
// kernel's hci debugfs knobs so BlueZ uses fast timings (100–200 ms) instead
// of the 1.28 s default. Values are in 0.625 ms units. Best-effort: any error
// (debugfs not mounted, different hci index, kernel without these knobs) is
// logged and we proceed with whatever BlueZ chooses.
func tuneAdvIntervals() {
	const minVal = "160" // 100 ms
	const maxVal = "320" // 200 ms

	// Try every hci<n> debugfs dir so this works on boards where the
	// controller isn't always hci0.
	matches, err := filepath.Glob("/sys/kernel/debug/bluetooth/hci*")
	if err != nil || len(matches) == 0 {
		log.Printf("[ble] WARN: bluetooth debugfs not available — using BlueZ default 1280ms advertising")
		return
	}

	for _, dir := range matches {
		minPath := filepath.Join(dir, "adv_min_interval")
		maxPath := filepath.Join(dir, "adv_max_interval")
		// Order matters: kernel rejects min > current max, so write max first
		// when raising, min first when lowering. We only ever lower, so
		// writing min first is safe (new min < old max=2048).
		if err := os.WriteFile(minPath, []byte(minVal), 0644); err != nil {
			log.Printf("[ble] WARN: tune %s: %v", minPath, err)
			continue
		}
		if err := os.WriteFile(maxPath, []byte(maxVal), 0644); err != nil {
			log.Printf("[ble] WARN: tune %s: %v", maxPath, err)
			continue
		}
		log.Printf("[ble] tuned advertising interval on %s: min=%s max=%s (units of 0.625ms)", dir, minVal, maxVal)
	}
}
