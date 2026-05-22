package openclaw

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"go-lamp.autonomous.ai/domain"
)

// Pairing-flow tunables. Mirrors lobster `lib/openclaw/pairing.go`.
const (
	whatsappPairingTimeout = 90 * time.Second
	whatsappPairingMaxQRs  = 5
	whatsappQRTTL          = 20 * time.Second
	// whatsappPostPairSyncDelay is how long we wait after the CLI prints
	// "✅ Linked" before emitting `success`. Baileys' post-pair sync (pre-keys,
	// history, contacts, presence) runs in the openclaw daemon AFTER the login
	// CLI exits and is not visible to us — so we approximate "messages ready"
	// with this fixed delay. Empirically 5 minutes covers the worst case.
	whatsappPostPairSyncDelay = 5 * time.Minute

	// whatsappPluginPackage is the npm package name for the externalized
	// WhatsApp plugin (used on openclaw 2026.5.x+; earlier bundled releases
	// already ship the channel and `plugins enable` succeeds directly).
	whatsappPluginPackage = "@openclaw/whatsapp"
)

// Per-process mutex: only one pairing flow runs at a time. The CLI binds
// Baileys to a single session; concurrent invocations race over openclaw.json
// and the credentials dir.
var (
	whatsappPairingMu     sync.Mutex
	whatsappPairingActive bool
)

// HasWhatsappSession reports whether a Baileys session exists on disk for the
// given account. Empty account resolves to "default". Used to skip pairing
// when the gateway can auto-resume an existing link.
func (s *Service) HasWhatsappSession(account string) bool {
	if account == "" {
		account = "default"
	}
	credsPath := filepath.Join(s.config.OpenclawConfigDir, "credentials", domain.ChannelWhatsapp, account, "creds.json")
	info, err := os.Stat(credsPath)
	return err == nil && info.Size() > 0
}

// PairWhatsapp runs `openclaw channels login --channel whatsapp` and emits
// PairingEvents on the returned channel. The channel is closed once the
// subprocess exits or the wall-clock cap fires. At most one pairing flow may
// be active; concurrent calls return a one-event channel containing
// PairingStatusFailure with error "pairing_already_in_progress".
//
// Caller MUST drain the channel; goroutine writes are buffered (capacity 8)
// but will block once buffer fills.
func (s *Service) PairWhatsapp(ctx context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 8)

	whatsappPairingMu.Lock()
	if whatsappPairingActive {
		whatsappPairingMu.Unlock()
		ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: "pairing_already_in_progress"}
		close(ch)
		return ch
	}
	whatsappPairingActive = true
	whatsappPairingMu.Unlock()

	go func() {
		defer func() {
			close(ch)
			whatsappPairingMu.Lock()
			whatsappPairingActive = false
			whatsappPairingMu.Unlock()
		}()
		s.runPairingProcess(ctx, ch)
	}()

	return ch
}

// runPairingProcess spawns the login CLI, scans its stdout for QR blocks and
// terminal markers, and emits PairingEvents. Returns when the subprocess exits
// or the context is cancelled.
func (s *Service) runPairingProcess(ctx context.Context, ch chan<- domain.PairingEvent) {
	runCtx, cancel := context.WithTimeout(ctx, whatsappPairingTimeout)
	defer cancel()

	cmd := exec.CommandContext(runCtx, "openclaw", "channels", "login", "--channel", domain.ChannelWhatsapp)

	pr, pw := io.Pipe()
	cmd.Stdout = pw
	cmd.Stderr = pw

	if err := cmd.Start(); err != nil {
		_ = pw.Close()
		ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: fmt.Sprintf("start pairing CLI: %v", err)}
		return
	}

	waitErr := make(chan error, 1)
	go func() {
		waitErr <- cmd.Wait()
		_ = pw.Close()
	}()

	ch <- domain.PairingEvent{Status: domain.PairingStatusStarting}

	linked := scanPairingStdout(pr, ch)

	err := <-waitErr
	if linked {
		// CLI confirmed QR scan, but Baileys' post-pair sync (pre-keys,
		// history, contacts) still runs in the openclaw daemon for some time
		// after the CLI exits. Wait a fixed window before declaring success
		// so the operator sees `success` only when WhatsApp messages can
		// actually be sent. Use the parent ctx (not runCtx, bounded by the
		// QR-scan timeout) so the post-scan delay isn't truncated.
		select {
		case <-time.After(whatsappPostPairSyncDelay):
		case <-ctx.Done():
			ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: fmt.Sprintf("cancelled during post-pair sync: %v", ctx.Err())}
			return
		}
		ch <- domain.PairingEvent{Status: domain.PairingStatusSuccess}
		return
	}
	switch {
	case runCtx.Err() == context.DeadlineExceeded:
		ch <- domain.PairingEvent{Status: domain.PairingStatusTimeout, Error: fmt.Sprintf("no scan within %s", whatsappPairingTimeout)}
	case err != nil:
		ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: fmt.Sprintf("pairing CLI exited: %v", err)}
	default:
		ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: "pairing CLI exited without confirmation"}
	}
}

// scanPairingStdout reads lines from the CLI process and emits intermediate
// PairingEvents (pairing_qr, intermediate timeouts on QR overflow). Returns
// true when the CLI prints "✅ Linked" — the caller is then responsible for
// waiting out the Baileys post-pair sync before emitting `success`.
func scanPairingStdout(r io.Reader, ch chan<- domain.PairingEvent) bool {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, 64*1024), 1<<20)

	var qrBuf bytes.Buffer
	qrSeq := 0
	inQR := false

	flush := func() {
		if qrBuf.Len() == 0 {
			return
		}
		qrSeq++
		ch <- domain.PairingEvent{
			Status:    domain.PairingStatusQR,
			QRText:    strings.TrimRight(qrBuf.String(), "\n"),
			QRSeq:     qrSeq,
			ExpiresAt: time.Now().UTC().Add(whatsappQRTTL),
		}
		qrBuf.Reset()
		if qrSeq >= whatsappPairingMaxQRs {
			ch <- domain.PairingEvent{Status: domain.PairingStatusTimeout, Error: fmt.Sprintf("operator did not scan within %d QR rotations", whatsappPairingMaxQRs)}
		}
	}

	for scanner.Scan() {
		line := scanner.Text()
		slog.Debug("whatsapp-pair stdout", "component", "openclaw", "line", line)

		switch {
		case strings.Contains(line, "Scan this QR in WhatsApp"):
			flush()
			inQR = true
		case strings.HasPrefix(line, "✅ Linked"):
			flush()
			return true
		case isQRLine(line):
			if inQR {
				qrBuf.WriteString(line)
				qrBuf.WriteByte('\n')
			}
		default:
			if inQR && strings.TrimSpace(line) != "" {
				flush()
				inQR = false
			}
		}
	}
	flush()
	return false
}

// isQRLine reports whether a line is a row of the QR ASCII rendering.
// Heuristic: only the four block runes Baileys uses ('█' '▀' '▄' ' '), length ≥30.
func isQRLine(line string) bool {
	if len(line) < 30 {
		return false
	}
	for _, r := range line {
		switch r {
		case '█', '▀', '▄', ' ':
			continue
		default:
			return false
		}
	}
	return true
}

// runOpenclawCLI shells out to the openclaw CLI and surfaces stdout/stderr in
// the error message. Used for `channels add`, `plugins install`, `plugins
// enable` from the WhatsApp add_channel path.
func runOpenclawCLI(ctx context.Context, args ...string) error {
	cmd := exec.CommandContext(ctx, "openclaw", args...)
	out, err := cmd.CombinedOutput()
	output := strings.TrimSpace(string(out))
	if err != nil {
		return fmt.Errorf("openclaw %s: %w (output: %s)", strings.Join(args, " "), err, output)
	}
	if output != "" {
		slog.Info("openclaw cli", "component", "openclaw", "args", strings.Join(args, " "), "output", output)
	}
	return nil
}

// applyWhatsappChannelConfig overlays the canonical channels.whatsapp block
// onto the map produced by `openclaw channels add` (which seeds defaults like
// accounts.default, mediaMaxMb). Pre-existing keys are preserved.
func applyWhatsappChannelConfig(whatsappMap map[string]any, userID string) {
	whatsappMap["enabled"] = true
	if userID == "" {
		// ValidateChannel rejects this, but stay defensive.
		whatsappMap["dmPolicy"] = "pairing"
		return
	}
	whatsappMap["dmPolicy"] = "allowlist"
	whatsappMap["allowFrom"] = mergeStringList(whatsappMap["allowFrom"], userID)
	whatsappMap["groupPolicy"] = "allowlist"
	whatsappMap["groupAllowFrom"] = mergeStringList(whatsappMap["groupAllowFrom"], userID)
	accountsMap := ensureMap(whatsappMap, "accounts")
	defaultAccount := ensureMap(accountsMap, "default")
	defaultAccount["enabled"] = true
	defaultAccount["dmPolicy"] = "allowlist"
	defaultAccount["allowFrom"] = mergeStringList(defaultAccount["allowFrom"], userID)
	accountsMap["default"] = defaultAccount
	whatsappMap["accounts"] = accountsMap
}
