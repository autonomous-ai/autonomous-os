package claudecode

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/server/config"
)

// Compile-time check: the claudecode backend is the ClaudeLoginPairer.
var _ domain.ClaudeLoginPairer = (*ClaudeCodeService)(nil)

// claude.ai OAuth login flow — the claudecode implementation of
// domain.ClaudeLoginPairer. Mirrors the WhatsApp pairing flow
// (internal/openclaw/pairing.go): spawn the CLI, scan its output, stream
// domain.PairingEvents. The one structural difference is the extra inbound leg:
// `claude setup-token` prints an authorization URL, the user opens it in a
// browser and copies a code back, and SubmitClaudeLoginCode feeds that code to
// the waiting CLI.
//
// On success the long-lived token (sk-ant-oat01-…) is persisted to config.json
// (claude_code_oauth_token) and EnsureOnboarding re-runs presync, which flips
// the launch env to subscription auth: CLAUDE_CODE_OAUTH_TOKEN is injected and
// the ANTHROPIC_* API-key vars are OMITTED — they outrank OAuth in Claude
// Code's credential precedence, so leaving them set would silently keep the
// device on the llm_api_key path.

const (
	// claudeLoginTimeout caps the whole flow — the user has to open the URL on
	// another device and paste the code back, so this is generous.
	claudeLoginTimeout = 10 * time.Minute

	// claudeCredentialsPath is where the CLI stores OAuth credentials on Linux
	// (no keychain). Its presence after the CLI exits is the success fallback
	// when the token line was not captured from the pty stream.
	claudeCredentialsPath = "/root/.claude/.credentials.json"
)

// loginURLRe matches the claude.ai / console authorization URL the CLI prints.
// Broad on host (claude.ai today, console.anthropic.com historically) but
// anchored on an oauth-ish path so incidental doc links don't false-positive.
var loginURLRe = regexp.MustCompile(`https://[^\s"']+(?:oauth|authorize)[^\s"']*`)

// loginTokenRe matches the long-lived OAuth token `claude setup-token` prints.
var loginTokenRe = regexp.MustCompile(`sk-ant-oat01-[A-Za-z0-9_-]+`)

// ansiRe strips ANSI escape sequences (CSI + OSC) — the CLI renders its
// interactive prompt through a pty, so the stream is full of them.
var ansiRe = regexp.MustCompile(`\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07`)

// Per-process mutex: only one login flow runs at a time (the CLI owns
// ~/.claude credentials; concurrent flows would race). claudeLoginStdin is the
// waiting CLI's pty stdin while a flow is active — SubmitClaudeLoginCode
// writes the pasted code there.
var (
	claudeLoginMu     sync.Mutex
	claudeLoginActive bool
	claudeLoginStdin  io.Writer
)

// StartClaudeLogin implements domain.ClaudeLoginPairer. Runs
// `claude setup-token` and emits PairingEvents on the returned channel:
// pairing_starting → pairing_url → success | timeout | failure. The channel is
// closed once the flow ends. Caller MUST drain; writes are buffered (capacity
// 8) but block once the buffer fills.
func (s *ClaudeCodeService) StartClaudeLogin(ctx context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 8)

	claudeLoginMu.Lock()
	if claudeLoginActive {
		claudeLoginMu.Unlock()
		ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: "login_already_in_progress"}
		close(ch)
		return ch
	}
	claudeLoginActive = true
	claudeLoginMu.Unlock()

	go func() {
		defer func() {
			close(ch)
			claudeLoginMu.Lock()
			claudeLoginActive = false
			claudeLoginStdin = nil
			claudeLoginMu.Unlock()
		}()
		s.runLoginProcess(ctx, ch)
	}()

	return ch
}

// SubmitClaudeLoginCode implements domain.ClaudeLoginPairer: feeds the
// authorization code the user copied from the browser to the waiting CLI.
// The code is terminated with "\r" — the pty is in raw mode, where Enter is a
// carriage return, not a newline.
func (s *ClaudeCodeService) SubmitClaudeLoginCode(code string) error {
	code = strings.TrimSpace(code)
	if code == "" {
		return fmt.Errorf("authorization code is required")
	}
	claudeLoginMu.Lock()
	defer claudeLoginMu.Unlock()
	if claudeLoginStdin == nil {
		return fmt.Errorf("no claude login in progress")
	}
	if _, err := io.WriteString(claudeLoginStdin, code+"\r"); err != nil {
		return fmt.Errorf("write code to login CLI: %w", err)
	}
	slog.Info("claude login: authorization code submitted", "component", "claudecode-login", "codeLen", len(code))
	return nil
}

// runLoginProcess spawns `claude setup-token` under a pty, scans its output,
// and emits PairingEvents. `setup-token` refuses to run without a TTY (its
// prompt is interactive), so it is wrapped in util-linux `script -qec` — the
// standard pty allocator on the device images; script forwards our stdin to
// the pty, which is how the pasted code reaches the CLI.
func (s *ClaudeCodeService) runLoginProcess(ctx context.Context, ch chan<- domain.PairingEvent) {
	runCtx, cancel := context.WithTimeout(ctx, claudeLoginTimeout)
	defer cancel()

	cmd := exec.CommandContext(runCtx, "script", "-qec", "claude setup-token", "/dev/null")
	cmd.Env = append(os.Environ(), "HOME=/root")

	stdin, err := cmd.StdinPipe()
	if err != nil {
		ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: fmt.Sprintf("stdin pipe: %v", err)}
		return
	}
	pr, pw := io.Pipe()
	cmd.Stdout = pw
	cmd.Stderr = pw

	if err := cmd.Start(); err != nil {
		_ = pw.Close()
		ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: fmt.Sprintf("start login CLI: %v", err)}
		return
	}

	claudeLoginMu.Lock()
	claudeLoginStdin = stdin
	claudeLoginMu.Unlock()

	waitErr := make(chan error, 1)
	go func() {
		waitErr <- cmd.Wait()
		_ = pw.Close()
	}()

	ch <- domain.PairingEvent{Status: domain.PairingStatusStarting}

	scan := scanLoginOutput(pr, ch)
	err = <-waitErr

	token := scan.token
	if token == "" && credentialsOnDisk() {
		// The CLI saved ~/.claude/.credentials.json but the token line was not
		// captured from the stream — presync also accepts the credentials file
		// as the subscription-auth signal, so this still counts as success.
		scan.sawSuccess = true
	}

	switch {
	case token != "" || scan.sawSuccess:
		if perr := s.adoptOAuthToken(token); perr != nil {
			ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: fmt.Sprintf("persist oauth token: %v", perr)}
			return
		}
		ch <- domain.PairingEvent{Status: domain.PairingStatusSuccess}
	case runCtx.Err() == context.DeadlineExceeded:
		ch <- domain.PairingEvent{Status: domain.PairingStatusTimeout, Error: fmt.Sprintf("no login within %s", claudeLoginTimeout)}
	case err != nil:
		ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: fmt.Sprintf("login CLI exited: %v (last output: %s)", err, scan.lastLine)}
	default:
		ch <- domain.PairingEvent{Status: domain.PairingStatusFailure, Error: fmt.Sprintf("login CLI exited without a token (last output: %s)", scan.lastLine)}
	}
}

// adoptOAuthToken persists the token to config.json and re-runs onboarding so
// presync rewrites /root/.claudecode/.env for subscription auth and the bridge
// restarts into it. An empty token is valid — credentials.json carries the
// auth and presync detects it on disk.
func (s *ClaudeCodeService) adoptOAuthToken(token string) error {
	if token != "" {
		if err := s.config.WithLockSave(func(c *config.Config) { c.ClaudeCodeOAuthToken = token }); err != nil {
			return fmt.Errorf("save config: %w", err)
		}
	}
	slog.Info("claude login: adopting subscription auth", "component", "claudecode-login", "token_captured", token != "")
	if err := s.EnsureOnboarding(); err != nil {
		return fmt.Errorf("apply subscription auth: %w", err)
	}
	return nil
}

// loginScan is what scanLoginOutput extracted from the CLI stream.
type loginScan struct {
	token      string
	sawSuccess bool
	lastLine   string
}

// scanLoginOutput reads the pty stream, emits pairing_url once when the
// authorization URL appears, and captures the token / success marker. The pty
// renders an interactive prompt, so lines are split on BOTH \n and \r (clack
// redraws in place with bare carriage returns) and ANSI escapes are stripped
// before matching.
func scanLoginOutput(r io.Reader, ch chan<- domain.PairingEvent) loginScan {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, 64*1024), 1<<20)
	scanner.Split(splitPTYLines)

	var out loginScan
	urlSent := false
	for scanner.Scan() {
		line := stripANSI(scanner.Text())
		if strings.TrimSpace(line) == "" {
			continue
		}
		out.lastLine = strings.TrimSpace(line)
		slog.Debug("claude-login output", "component", "claudecode-login", "line", out.lastLine)

		if !urlSent {
			if m := loginURLRe.FindString(line); m != "" {
				urlSent = true
				ch <- domain.PairingEvent{Status: domain.PairingStatusURL, URL: m}
			}
		}
		if out.token == "" {
			if m := loginTokenRe.FindString(line); m != "" {
				out.token = m
				out.sawSuccess = true
			}
		}
		// Textual success markers, in case the token render is ever elided.
		lower := strings.ToLower(line)
		if strings.Contains(lower, "successfully logged in") ||
			strings.Contains(lower, "token created") ||
			strings.Contains(lower, "login successful") {
			out.sawSuccess = true
		}
	}
	return out
}

// splitPTYLines is a bufio.SplitFunc that treats both \n and \r as line
// terminators — interactive CLIs redraw prompt lines with bare carriage
// returns, which a plain line scanner would buffer until process exit.
func splitPTYLines(data []byte, atEOF bool) (advance int, token []byte, err error) {
	if atEOF && len(data) == 0 {
		return 0, nil, nil
	}
	if i := bytes.IndexAny(data, "\r\n"); i >= 0 {
		return i + 1, data[:i], nil
	}
	if atEOF {
		return len(data), data, nil
	}
	return 0, nil, nil
}

// stripANSI removes ANSI escape sequences so regex matching sees plain text.
func stripANSI(s string) string {
	return ansiRe.ReplaceAllString(s, "")
}

// credentialsOnDisk reports whether the CLI persisted OAuth credentials.
func credentialsOnDisk() bool {
	info, err := os.Stat(claudeCredentialsPath)
	return err == nil && info.Size() > 0
}
