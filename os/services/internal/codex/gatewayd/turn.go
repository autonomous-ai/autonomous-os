package gatewayd

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

// turnResult mirrors bridge.py's per-run result dict.
type turnResult struct {
	rc            int
	threadStarted bool
	turnEnded     bool // saw turn.completed or turn.failed
	timedOut      bool
	spawnFailed   bool
	stderrTail    string // bounded tail of stderr
	stdoutTail    string // bounded tail of non-JSON stdout (resume heuristics)
}

// turnWorker drains the queue one turn at a time (strict serialization).
func (s *Server) turnWorker(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case payload := <-s.turns:
			s.runTurn(ctx, payload)
		}
	}
}

// runTurn executes one message.send: decode attachments, spawn codex exec
// (resuming the stored thread when present), retry once fresh if the resume
// target is gone, and surface terminal failures as bridge.error frames.
func (s *Server) runTurn(ctx context.Context, payload turnPayload) {
	images := s.decodeAttachments(payload)
	s.pruneAttachments()

	s.mu.Lock()
	resumeID := s.threadID
	s.mu.Unlock()

	start := time.Now()
	log.Printf("%s turn start thread=%q resumed=%v images=%d",
		logPrefix, resumeID, resumeID != "", len(images))

	res := s.execTurn(ctx, payload.Content, images, resumeID)
	if resumeID != "" && resumeFailed(res) {
		log.Printf("%s resume of thread %s failed (rc=%d) — retrying fresh",
			logPrefix, resumeID, res.rc)
		s.clearSession()
		res = s.execTurn(ctx, payload.Content, images, "")
	}

	switch {
	case res.timedOut:
		s.sendError("timeout")
	case !res.turnEnded:
		errMsg := strings.TrimSpace(res.stderrTail)
		if errMsg == "" {
			errMsg = fmt.Sprintf("codex exited rc=%d without turn.completed", res.rc)
		}
		if len(errMsg) > stderrTailMax {
			errMsg = errMsg[len(errMsg)-stderrTailMax:]
		}
		s.sendError(errMsg)
	}

	s.mu.Lock()
	threadID := s.threadID
	s.mu.Unlock()
	log.Printf("%s turn end thread=%q resumed=%v duration=%s rc=%d",
		logPrefix, threadID, resumeID != "", time.Since(start).Round(time.Millisecond), res.rc)
}

// resumeFailed reports whether a failed resumed run should be retried fresh:
// the run failed (nonzero exit or no terminal event — checked by the caller
// path below), was not a timeout/spawn failure, and either never started a
// thread or the output mentions the session/thread cannot be found.
func resumeFailed(res turnResult) bool {
	if res.timedOut || res.spawnFailed {
		return false
	}
	if res.rc == 0 && res.turnEnded {
		return false // run succeeded — nothing to retry
	}
	if !res.threadStarted {
		return true
	}
	low := strings.ToLower(res.stderrTail + "\n" + res.stdoutTail)
	for _, hint := range resumeErrHints {
		if strings.Contains(low, hint) {
			return true
		}
	}
	return false
}

// buildArgv builds the codex exec command line. The prompt is always the
// final positional argument; image paths ride repeated -i flags.
func (s *Server) buildArgv(prompt string, images []string, resumeID string) []string {
	argv := []string{s.cfg.CodexBin, "exec"}
	if resumeID != "" {
		argv = append(argv, "resume", resumeID)
	}
	argv = append(argv, "--json", "--dangerously-bypass-approvals-and-sandbox",
		"--cd", s.cfg.Workspace)
	for _, img := range images {
		argv = append(argv, "-i", img)
	}
	return append(argv, prompt)
}

// turnEnv is os.Environ() with HOME and CODEX_HOME asserted (deduped).
func (s *Server) turnEnv() []string {
	env := os.Environ()
	out := make([]string, 0, len(env)+2)
	for _, kv := range env {
		if strings.HasPrefix(kv, "HOME=") || strings.HasPrefix(kv, "CODEX_HOME=") {
			continue
		}
		out = append(out, kv)
	}
	return append(out, "HOME="+s.cfg.Home, "CODEX_HOME="+s.cfg.CodexHome)
}

// execTurn spawns one `codex exec` subprocess for the turn, forwards its
// JSONL stdout verbatim to the client and returns the bounded result.
func (s *Server) execTurn(ctx context.Context, prompt string, images []string, resumeID string) turnResult {
	res := turnResult{rc: -1}
	argv := s.buildArgv(prompt, images, resumeID)
	log.Printf("%s spawning: %s <prompt %d chars>",
		logPrefix, strings.Join(argv[:len(argv)-1], " "), len(prompt))

	tctx, cancel := context.WithTimeout(ctx, s.cfg.TurnTimeout)
	defer cancel()

	cmd := exec.CommandContext(tctx, argv[0], argv[1:]...)
	cmd.Dir = s.cfg.Workspace
	cmd.Env = s.turnEnv()
	// Own process group so a timeout kill reaps codex AND its children.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if p := cmd.Process; p != nil {
			if err := syscall.Kill(-p.Pid, syscall.SIGKILL); err == nil {
				return nil
			}
			return p.Kill()
		}
		return nil
	}

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		res.spawnFailed = true
		res.stderrTail = "stdout pipe: " + err.Error()
		return res
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		res.spawnFailed = true
		res.stderrTail = "stderr pipe: " + err.Error()
		return res
	}
	if err := cmd.Start(); err != nil {
		log.Printf("%s spawn codex failed: %v", logPrefix, err)
		res.spawnFailed = true
		res.stderrTail = "spawn codex failed: " + err.Error()
		return res
	}

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); s.pumpStdout(stdout, &res) }()
	go func() { defer wg.Done(); pumpStderr(stderr, &res) }()
	wg.Wait()
	err = cmd.Wait()

	res.rc = cmd.ProcessState.ExitCode()
	if err != nil && res.rc == 0 {
		res.rc = -1 // killed / wait error without a real exit code
	}
	if tctx.Err() == context.DeadlineExceeded {
		log.Printf("%s turn timed out after %s — killed process group",
			logPrefix, s.cfg.TurnTimeout)
		res.timedOut = true
	}
	log.Printf("%s codex exited rc=%d", logPrefix, res.rc)
	return res
}

// pumpStdout forwards each JSON stdout line verbatim to the client while
// watching for thread.started (persist session) and terminal turn events.
func (s *Server) pumpStdout(r io.Reader, res *turnResult) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, scanBufSize), streamLimit)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		if !json.Valid([]byte(line)) {
			log.Printf("%s debug: non-JSON stdout (not forwarded): %.200s", logPrefix, line)
			res.stdoutTail = tail(res.stdoutTail+line+"\n", stderrTailMax)
			continue
		}
		var evt struct {
			Type     string `json:"type"`
			ThreadID string `json:"thread_id"`
		}
		if json.Unmarshal([]byte(line), &evt) == nil {
			switch evt.Type {
			case "thread.started":
				res.threadStarted = true
				if evt.ThreadID != "" {
					s.storeThreadID(evt.ThreadID)
				}
			case "turn.completed", "turn.failed":
				res.turnEnded = true
			}
		}
		s.send([]byte(line))
	}
	if err := scanner.Err(); err != nil {
		log.Printf("%s stdout scan error: %v", logPrefix, err)
	}
}

// pumpStderr logs stderr lines and keeps a bounded tail for diagnostics.
func pumpStderr(r io.Reader, res *turnResult) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, scanBufSize), streamLimit)
	for scanner.Scan() {
		line := strings.TrimRight(scanner.Text(), " \t\r")
		log.Printf("%s codex: %s", logPrefix, line)
		res.stderrTail = tail(res.stderrTail+line+"\n", stderrTailMax)
	}
}

func tail(s string, max int) string {
	if len(s) > max {
		return s[len(s)-max:]
	}
	return s
}

// -- image attachments -------------------------------------------------------

// decodeAttachments writes data-URL images to files and returns their paths;
// codex exec has no stdin image channel, so paths ride repeated -i flags and
// never enter the prompt.
func (s *Server) decodeAttachments(payload turnPayload) []string {
	var paths []string
	nowMS := time.Now().UnixMilli()
	for i, att := range payload.Attachments {
		if !strings.HasPrefix(att.URL, "data:") {
			continue
		}
		comma := strings.Index(att.URL, ",")
		if comma < 0 {
			log.Printf("%s bad image attachment, skipped", logPrefix)
			continue
		}
		data, err := base64.StdEncoding.DecodeString(att.URL[comma+1:])
		if err != nil {
			log.Printf("%s bad image attachment, skipped", logPrefix)
			continue
		}
		if err := os.MkdirAll(s.cfg.AttachDir, 0o755); err != nil {
			log.Printf("%s write attachment failed: %v", logPrefix, err)
			continue
		}
		path := filepath.Join(s.cfg.AttachDir, fmt.Sprintf("attach-%d-%d.jpg", nowMS, i))
		if err := os.WriteFile(path, data, 0o600); err != nil {
			log.Printf("%s write attachment failed: %v", logPrefix, err)
			continue
		}
		paths = append(paths, path)
	}
	return paths
}

// pruneAttachments drops attachments older than attachMaxAge (best-effort).
func (s *Server) pruneAttachments() {
	cutoff := time.Now().Add(-attachMaxAge)
	entries, err := os.ReadDir(s.cfg.AttachDir)
	if err != nil {
		return
	}
	for _, e := range entries {
		info, err := e.Info()
		if err != nil {
			continue
		}
		if info.ModTime().Before(cutoff) {
			_ = os.Remove(filepath.Join(s.cfg.AttachDir, e.Name()))
		}
	}
}

// -- session persistence -----------------------------------------------------

// loadSession reads the persisted thread id (absent/corrupt file -> "").
func (s *Server) loadSession() string {
	data, err := os.ReadFile(s.cfg.SessionFile)
	if err != nil {
		return ""
	}
	var sess struct {
		ThreadID string `json:"thread_id"`
	}
	if err := json.Unmarshal(data, &sess); err != nil {
		return ""
	}
	return sess.ThreadID
}

// storeThreadID persists a changed thread id atomically (temp + rename).
func (s *Server) storeThreadID(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if id == s.threadID {
		return
	}
	s.threadID = id
	data, _ := json.Marshal(map[string]string{"thread_id": id})
	dir := filepath.Dir(s.cfg.SessionFile)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		log.Printf("%s save session failed: %v", logPrefix, err)
		return
	}
	tmp, err := os.CreateTemp(dir, ".session-*.json")
	if err != nil {
		log.Printf("%s save session failed: %v", logPrefix, err)
		return
	}
	tmpName := tmp.Name()
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		_ = os.Remove(tmpName)
		log.Printf("%s save session failed: %v", logPrefix, err)
		return
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(tmpName)
		log.Printf("%s save session failed: %v", logPrefix, err)
		return
	}
	if err := os.Rename(tmpName, s.cfg.SessionFile); err != nil {
		_ = os.Remove(tmpName)
		log.Printf("%s save session failed: %v", logPrefix, err)
	}
}

// clearSession forgets the thread id and deletes the session file.
func (s *Server) clearSession() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.threadID = ""
	if err := os.Remove(s.cfg.SessionFile); err != nil && !os.IsNotExist(err) {
		log.Printf("%s remove session file failed: %v", logPrefix, err)
	}
}
