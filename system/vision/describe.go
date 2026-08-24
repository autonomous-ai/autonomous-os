// Package vision turns camera frames into text descriptions with a vision
// LLM, so image-bearing turns can reach a text-only main model.
//
// Why this exists: the main agent model (Auto-AI behind the campaign-api
// smart-agent-router) is text-only. A raw image can never reach it — an image
// block returned by the agent's read tool is silently dropped by the runtime
// (the agent then answers blind and hallucinates), and a chat.send attachment
// 404s at the router when it picks a no-vision backend ("No endpoints found
// that support image input"). Telegram photos work because openclaw's
// imageModel describes them first; this package gives the sensing/web-chat
// path the same describe-first treatment, one layer up, so every image source
// (HAL look-frame handoff, web monitor chat) converges on one fix.
//
// Once the smart-agent-router routes by modality (image → vision backend),
// callers can send attachments directly and this package can be removed.
package vision

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/runtimes/openclaw"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// DefaultImageModel is the fallback vision model when the catalog
// (GET {llm_base}/models) is unreachable — normally the catalog's
// `default_image_model` wins (same model openclaw's imageModel uses for
// Telegram photos).
const DefaultImageModel = "qwen/qwen3.6-plus"

// catalogTTL bounds how often the model catalog is re-fetched for the
// vision-capability gate — matches openclaw's ModelSyncInterval so a BE
// catalog flip propagates on the same cadence as the rest of the device.
const catalogTTL = 30 * time.Minute

// DescribeTimeout bounds the TOTAL describe budget across all attempts. It
// runs inline in the sensing handler before the agent forward, so it delays
// the turn by at most this long. Keep it under HAL's image-turn POST timeout
// (90s in sensing_sender.py) so the HAL client outlives describe + agent
// forward.
const DescribeTimeout = 80 * time.Second

// Per-attempt timeouts for DescribeWithRetry; they sum to DescribeTimeout.
// Sized from device measurements 2026-07-06 (Go client, 768px frame, same
// endpoint): latency is CONTENT-dependent — scene images answer in 8–20s,
// but TEXT-DENSE images (the "read this label" use case) take 23–38s because
// qwen3.6-plus reasons over the text; /no_think and max_tokens caps barely
// help. The old 20s/15s (and 30s/25s) ladders sat inside that band, so every
// text-reading turn timed out while probes with scene images passed. 45s
// clears the measured text-dense tail with margin; the 35s retry still gets
// a fresh connection for genuinely hung requests while staying above the
// text-dense floor.
var describeAttemptTimeouts = [...]time.Duration{45 * time.Second, 35 * time.Second}

// describeMaxTokens is the output budget for one describe call. It must cover
// the model's REASONING as well as the description: qwen3.6-plus (the catalog's
// default_image_model) thinks before it writes, and the thinking is billed to
// the same budget while producing no content block. Overrun it and the response
// is HTTP 200 with `content: []` and `stop_reason: "max_tokens"` — a
// deterministic empty answer, not a transient failure.
//
// Sized from device measurements 2026-08-24 (lamp-0c89, one 118 KB look frame,
// same prompt, 11 calls): the OLD 500 budget was cut every time (out=501). Total
// output on a successful call ranged 868–1677 tokens — nearly 2x spread on the
// SAME image — so the ceiling has to clear the tail, not the median. At 2000,
// 4/5 calls succeeded and the fifth was cut at exactly 2001; at 4000, 5/5
// succeeded with the worst case at 1617, leaving ~2.4x headroom. Text-dense
// frames ("read this label") were not in the sample and plausibly cost more,
// which is the other reason not to trim this to the observed maximum.
const describeMaxTokens = 4000

// Emphasis on the user's request so the description surfaces what the answer
// needs (label text, object identity, counts) instead of a generic caption.
const describePrompt = "Describe this photo concisely but completely: main objects, any readable " +
	"text/labels, people, colors, and layout. It was just captured by a device camera " +
	"to answer the user's request below, so emphasize whatever is relevant to that " +
	"request. Reply in the same language as the request.\n\nUser request: %s"

var httpClient = &http.Client{Timeout: DescribeTimeout}

// Cached model catalog for the vision-capability gate. Guarded by catalogMu;
// a failed refresh keeps serving the stale copy (nil until the first success).
var (
	catalogMu        sync.Mutex
	catalogCache     *domain.LLMModelsListResponse
	catalogFetchedAt time.Time
)

// fetchCatalog returns the cached model catalog, refreshing it at most every
// catalogTTL. fetch is injected so tests don't hit the network (production
// callers pass openclaw.FetchModelsFromAPI via the package-level hook below).
func fetchCatalog() *domain.LLMModelsListResponse {
	catalogMu.Lock()
	defer catalogMu.Unlock()
	if catalogCache != nil && time.Since(catalogFetchedAt) < catalogTTL {
		return catalogCache
	}
	resp, err := fetchModels()
	if err != nil {
		// Keep (possibly nil) stale copy; retry after TTL, not on every call.
		catalogFetchedAt = time.Now()
		return catalogCache
	}
	catalogCache = resp
	catalogFetchedAt = time.Now()
	return catalogCache
}

// fetchModels is swappable for tests.
var fetchModels = openclaw.FetchModelsFromAPI

// ModelSupportsVision reports whether the device's ACTIVE main model declares
// image input in the upstream catalog. The sensing handler uses this as the
// describe-first gate: a vision-capable main model should receive the raw
// attachment (full pixels, no extra hop), a text-only one gets the qwen
// description instead. Catalog-driven on purpose — the moment the backend
// flips the model's `input` to include "image", devices switch to direct
// attachments within one catalog TTL, no code change. Unknown model or
// unreachable catalog → false (describe-first, the safe default).
func ModelSupportsVision(cfg *config.Config) bool {
	catalog := fetchCatalog()
	if catalog == nil {
		return false
	}
	active := strings.TrimSpace(cfg.LLMModel)
	if active == "" {
		active = catalog.DefaultModel
	}
	for _, m := range catalog.Models {
		if m.Key != active {
			continue
		}
		for _, in := range m.Input {
			if strings.EqualFold(in, "image") {
				return true
			}
		}
		return m.Capabilities != nil && m.Capabilities.SupportsVision
	}
	return false
}

// imageModel returns the catalog's default_image_model, falling back to the
// baked-in default when the catalog is unreachable or silent about it.
func imageModel() string {
	if c := fetchCatalog(); c != nil && strings.TrimSpace(c.DefaultImageModel) != "" {
		return c.DefaultImageModel
	}
	return DefaultImageModel
}

// stopReasonMaxTokens is the anthropic-messages stop_reason for a response cut
// off at the output budget.
const stopReasonMaxTokens = "max_tokens"

// errBudget reports a response cut off at describeMaxTokens before the model
// wrote any content. Retrying it is pointless — see DescribeWithRetry.
type errBudget struct {
	tokens int
	limit  int
}

func (e errBudget) Error() string {
	return fmt.Sprintf("model spent the whole %d-token budget on reasoning and returned no content "+
		"(output_tokens=%d, stop_reason=%s) — raise describeMaxTokens",
		e.limit, e.tokens, stopReasonMaxTokens)
}

// DescribeWithRetry runs Describe once per describeAttemptTimeouts entry,
// returning the first success. Uses context.Background() on purpose: the
// description must complete even if the HAL client gives up on the sensing
// HTTP request early (its POST timeout can be shorter than a slow describe).
// On total failure the error carries every attempt's failure.
//
// A budget overrun (errBudget) stops the loop instead of retrying: the retry
// sends an identical request with an identical budget, so it fails identically.
// Device-observed 2026-08-24 — both attempts returned the same empty response
// and the turn paid 26s of dead air for a foregone conclusion. Every other
// failure (timeout, 5xx, dead connection) still gets its retry.
func DescribeWithRetry(cfg *config.Config, imageB64 string, question string) (string, error) {
	var errs []string
	for i, timeout := range describeAttemptTimeouts {
		dctx, cancel := context.WithTimeout(context.Background(), timeout)
		desc, err := Describe(dctx, cfg, imageB64, question)
		cancel()
		if err == nil {
			return desc, nil
		}
		errs = append(errs, fmt.Sprintf("attempt %d (%s): %v", i+1, timeout, err))
		var budget errBudget
		if errors.As(err, &budget) {
			break
		}
	}
	return "", fmt.Errorf("%s", strings.Join(errs, "; "))
}

// Describe sends the base64 JPEG to the vision model via the anthropic-messages
// endpoint derived from the device's LLM config and returns the description.
// The request shape matches what the Anthropic SDK produces (HAL's summarizer
// uses the same endpoint successfully): POST {llm_base minus /v1}/v1/messages
// with an x-api-key header.
func Describe(ctx context.Context, cfg *config.Config, imageB64 string, question string) (string, error) {
	base := strings.TrimSuffix(strings.TrimSpace(cfg.LLMBaseURL), "/")
	base = strings.TrimSuffix(base, "/v1")
	if base == "" || cfg.LLMAPIKey == "" {
		return "", fmt.Errorf("llm base url or api key not configured")
	}
	if len(question) > 500 {
		question = question[:500]
	}

	model := imageModel()
	body, err := json.Marshal(map[string]any{
		"model":      model,
		"max_tokens": describeMaxTokens,
		"messages": []any{
			map[string]any{
				"role": "user",
				"content": []any{
					map[string]any{
						"type": "image",
						"source": map[string]any{
							"type":       "base64",
							"media_type": "image/jpeg",
							"data":       imageB64,
						},
					},
					map[string]any{
						"type": "text",
						"text": fmt.Sprintf(describePrompt, question),
					},
				},
			},
		},
	})
	if err != nil {
		return "", fmt.Errorf("marshal describe request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/v1/messages", bytes.NewReader(body))
	if err != nil {
		return "", fmt.Errorf("build describe request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-api-key", cfg.LLMAPIKey)
	req.Header.Set("anthropic-version", "2023-06-01")

	resp, err := httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("describe request (model=%s): %w", model, err)
	}
	defer resp.Body.Close()
	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", fmt.Errorf("read describe response (model=%s): %w", model, err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("describe status %d (model=%s): %s", resp.StatusCode, model, truncate(string(respBody), 300))
	}

	var out struct {
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
		StopReason string `json:"stop_reason"`
		Usage      struct {
			OutputTokens int `json:"output_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(respBody, &out); err != nil {
		return "", fmt.Errorf("decode describe response: %w", err)
	}
	for _, c := range out.Content {
		if c.Type == "text" && strings.TrimSpace(c.Text) != "" {
			return strings.TrimSpace(c.Text), nil
		}
	}
	// No text block. Carry stop_reason and the token count: without them the
	// error reads the same whether the model was cut off mid-reasoning or
	// genuinely returned nothing, and telling those apart meant reproducing the
	// call by hand (device debug 2026-08-24).
	if out.StopReason == stopReasonMaxTokens {
		return "", errBudget{tokens: out.Usage.OutputTokens, limit: describeMaxTokens}
	}
	return "", fmt.Errorf("describe response has no text content (stop_reason=%q, output_tokens=%d)",
		out.StopReason, out.Usage.OutputTokens)
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
