package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/serializers"
)

// softwareUpdateLastFire tracks the last time each OTA target was triggered, so
// a stuck/looping caller can't kick off back-to-back force-checks. Bootstrap's
// downloader is idempotent but the resulting service restarts (os-server +
// systemd reload + journal noise) are not free; 30 s is enough to absorb a
// double-click without hiding genuine retries.
var (
	softwareUpdateLastFire   = map[string]time.Time{}
	softwareUpdateLastFireMu sync.Mutex
)

const softwareUpdateMinInterval = 30 * time.Second

// softwareUpdate triggers an OTA update for a single named component via the bootstrap worker.
// POST /api/system/software-update/:target
// target: os-server | web | hal | agent (resolves to the configured runtime's CLI)
func (s *Server) softwareUpdate(c *gin.Context) {
	target := c.Param("target")
	// "agent" is a virtual target: the caller (the Versions card) knows there is
	// an agent CLI but not WHICH one, so the runtime is resolved here rather than
	// shipped to the browser. It maps to the OTA key of the configured runtime —
	// they share the same names by construction (domain.AgentRuntime* ==
	// domain.OTAKey* for the CLIs).
	if target == "agent" {
		runtime := device.CurrentAgentRuntimeFromConfig(s.config)
		// Hermes is excluded on purpose: `hermes update` cannot be pinned to a
		// version, so bootstrap never auto-applies it (see domain/ota.go). A
		// button that silently did nothing would be worse than no button.
		if runtime == domain.AgentRuntimeHermes {
			c.JSON(http.StatusBadRequest, serializers.ResponseError("hermes cannot be updated over OTA — run `software-update hermes` on the device"))
			return
		}
		target = runtime
	}
	allowed := map[string]bool{
		"os-server": true, "web": true, "hal": true,
		domain.OTAKeyCodex: true, domain.OTAKeyClaudeCode: true, domain.OTAKeyOpenCode: true, domain.OTAKeyPicoClaw: true,
	}
	if !allowed[target] {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("unknown target: "+target))
		return
	}

	// Per-target rate limit. Returns 429 with retry-after so the web button
	// can surface a useful message instead of looking broken.
	softwareUpdateLastFireMu.Lock()
	if last, ok := softwareUpdateLastFire[target]; ok {
		if wait := softwareUpdateMinInterval - time.Since(last); wait > 0 {
			softwareUpdateLastFireMu.Unlock()
			c.Header("Retry-After", strconv.Itoa(int(wait.Seconds())+1))
			c.JSON(http.StatusTooManyRequests,
				serializers.ResponseError(fmt.Sprintf("software-update %s rate-limited, retry in %ds", target, int(wait.Seconds())+1)))
			return
		}
	}
	softwareUpdateLastFire[target] = time.Now()
	softwareUpdateLastFireMu.Unlock()

	url := "http://127.0.0.1:8080/force-check/" + target
	req, err := http.NewRequestWithContext(c.Request.Context(), http.MethodPost, url, nil)
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("build request: "+err.Error()))
		return
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("bootstrap unreachable: "+err.Error()))
		return
	}
	defer resp.Body.Close()
	// Propagate a refusal instead of reporting success: bootstrap keeps its own
	// target allowlist, so the two can disagree (they did while the agent CLIs
	// were being added). Without this the web button says "OK" for a check that
	// never ran.
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<10))
		c.JSON(http.StatusBadGateway, serializers.ResponseError(
			fmt.Sprintf("bootstrap refused %s: %s %s", target, resp.Status, strings.TrimSpace(string(body)))))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess("software update triggered: "+target))
}

// otaSecurity reports whether this device verifies OTA metadata and artifacts.
// GET /api/system/ota-security
//
// The bootstrap worker owns the answer (it holds the pinned key and performs
// the verification), so this handler proxies its /security endpoint verbatim
// rather than re-reading bootstrap.json and guessing.
func (s *Server) otaSecurity(c *gin.Context) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://127.0.0.1:8080/security", nil)
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("build request: "+err.Error()))
		return
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("bootstrap unreachable: "+err.Error()))
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("bootstrap security status: "+resp.Status))
		return
	}
	var status map[string]any
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&status); err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("decode security status: "+err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(status))
}

// execCommand runs a shell command (sh -c) and returns stdout, stderr, and exit code.
// POST /api/system/exec  body: {"cmd": "..."}
func (s *Server) execCommand(c *gin.Context) {
	var body struct {
		Cmd string `json:"cmd"`
	}
	if err := c.ShouldBindJSON(&body); err != nil || strings.TrimSpace(body.Cmd) == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("cmd required"))
		return
	}

	ctx, cancel := context.WithTimeout(c.Request.Context(), 30*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, "sh", "-c", body.Cmd)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	exitCode := 0
	if err := cmd.Run(); err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			exitCode = exitErr.ExitCode()
		} else {
			exitCode = -1
			if stderr.Len() == 0 {
				stderr.WriteString(err.Error())
			}
		}
	}

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"stdout":    stdout.String(),
		"stderr":    stderr.String(),
		"exit_code": exitCode,
	}))
}
