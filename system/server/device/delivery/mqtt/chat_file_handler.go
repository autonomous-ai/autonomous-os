package mqtthandler

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"go.autonomous.ai/os/system/agentfile"
	"go.autonomous.ai/os/system/domain"
)

// kind:"chat.file.get" — fetch ONE device-local file a turn named.
//
// A turn can only NAME the file it made: "take a photo" ends with an absolute
// path like /root/.openclaw/media/hal-snapshots/snap_*.jpg. The web chat renders
// that by spotting the path in the message it is drawing and fetching
// GET /api/agent/file. This is the same thing over MQTT, for a phone that cannot
// reach the device at all — CLIENT-DRIVEN pull, not a device-side push.
//
// Pull is the right shape here, and the reasons are the ones that make the web
// side work:
//
//   - It applies to messages the client already has. A conversation scrolled
//     back weeks still resolves its images; a push only ever covers the turn
//     that happens to be live.
//   - Files nobody opens cost nothing. Pushing every snapshot would spend the
//     device's uplink on images a user may never scroll to.
//   - One client implementation. A phone reuses the web chat's path regex and
//     its "fetch, and on failure just leave the path as text" behaviour.
//
// `path` comes from the client, so it is hostile input — validated by
// system/agentfile, the same allow-list GET /api/agent/file enforces.

// chatFileMaxInlineBytes caps a file carried inline on fd_channel. Well under
// agentfile.MaxBytes, which governs a same-network HTTP fetch: this is the
// device's uplink, shared with every other command, and base64 adds a third on
// top. A camera snapshot is ~40 KB, so the realistic case is nowhere near it.
const chatFileMaxInlineBytes = 2 << 20

func (h *DeviceMQTTHandler) handleChatFileGet(env domain.MQTTDataCommand) error {
	var req domain.MQTTChatFileGetData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		return h.publishDataResult(env.Kind, "failure", "invalid data: "+err.Error(), nil)
	}
	if strings.TrimSpace(req.Path) == "" {
		return h.publishDataResult(env.Kind, "failure", "path is required", nil)
	}

	data, err := buildChatFile(req)
	if err != nil {
		slog.Info("chat.file.get refused", "component", "mqtt-chat",
			"path", req.Path, "reason", err)
		return h.publishDataResult(env.Kind, "failure", err.Error(), domain.MQTTChatFileData{
			RunID:     req.RunID,
			SessionID: req.SessionID,
			Path:      req.Path,
		})
	}

	slog.Info("chat.file.get served", "component", "mqtt-chat",
		"path", req.Path, "size", data.Size, "too_large", data.TooLarge)
	return h.publishDataResult(env.Kind, "success", "", data)
}

// buildChatFile validates the requested path and builds the reply payload.
// Split from the handler so the allow-list and size behaviour are testable
// without a broker.
func buildChatFile(req domain.MQTTChatFileGetData) (domain.MQTTChatFileData, error) {
	resolved, mime, err := agentfile.Resolve(req.Path, agentfile.Roots())
	if err != nil {
		// Deliberately vague to the caller: the reason a path is refused (wrong
		// type vs outside the roots vs absent) tells a prober about the device's
		// filesystem. The real reason is logged.
		return domain.MQTTChatFileData{}, errors.New("file not available")
	}

	info, err := os.Stat(resolved)
	if err != nil {
		return domain.MQTTChatFileData{}, errors.New("file not available")
	}

	out := domain.MQTTChatFileData{
		RunID:     req.RunID,
		SessionID: req.SessionID,
		Name:      filepath.Base(resolved),
		Path:      req.Path,
		MIME:      mime,
		Size:      info.Size(),
	}

	// Past the inline budget the metadata still comes back, so a client can say
	// "a 12 MB video" rather than show nothing.
	if info.Size() > chatFileMaxInlineBytes {
		out.TooLarge = true
		return out, nil
	}

	body, err := os.ReadFile(resolved)
	if err != nil {
		return domain.MQTTChatFileData{}, errors.New("file not available")
	}
	// base64 mirrors how a chat.send carries an inbound image, so the backend
	// handles one encoding in both directions.
	out.Content = base64.StdEncoding.EncodeToString(body)
	return out, nil
}
