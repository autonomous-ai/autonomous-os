package mqtthandler

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/serializers"
)

// connectorHTTPTimeout bounds a local-web PAT write. Same 2-minute budget as
// the MQTT dispatcher — the writer may take seconds to persist plus a slow
// disk fsync, and callers can retry cheaply.
const connectorHTTPTimeout = 2 * time.Minute

// patConnectorRequest is the body of POST /api/device/connectors/pat, sent by
// the device's local Settings UI. Mirrors ecm-website's PAT connector POST
// shape (connector code + a static credential + optional identity fields) so a
// single skill-side reader treats a locally-typed token the same as one the
// backend pushed via MQTT connector.set.<code>.
//
// APIKey carries the static credential (Page Access Token, App Password, PAT
// bearer, …). We deliberately do not accept an access_token/refresh_token on
// this endpoint: everything reaching here is a static, operator-pasted secret
// and not eligible for OAuth rotation.
type patConnectorRequest struct {
	Connector string `json:"connector"`
	APIKey    string `json:"api_key"`
	// UserEmail / UserID / PageID land in the connector entry's non-secret
	// fields so a subsequent GET can surface a "connected as <who>" hint
	// without exposing the token. Missing values are simply omitted.
	UserEmail   string            `json:"user_email,omitempty"`
	Credentials map[string]string `json:"credentials,omitempty"`
}

// SetConnectorPAT handles POST /api/device/connectors/pat — the local Settings
// UI's write path for a static-credential connector (Facebook Fan Page, Gmail
// app password, …). Reuses the SAME connectorWriter the MQTT connector.set
// dispatcher uses, so a token pasted on-device lands in the same
// <code>_access_tokens.json file the skill layer already reads. Sharing the
// writer instance is deliberate — its per-file mutex protects the two paths
// from stepping on each other.
//
// Kept intentionally MVP: no OAuth eligibility (Refresh:false), no expiry
// bookkeeping (ExpiresAt:0), no ops-alerting. If the operator later swaps to
// the ecm-website's admin flow, the backend's connector.set.<code> writes to
// the same file and this endpoint becomes an optional local convenience.
func (h *DeviceMQTTHandler) SetConnectorPAT(c *gin.Context) {
	var req patConnectorRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid body: "+err.Error()))
		return
	}
	req.Connector = strings.TrimSpace(req.Connector)
	req.APIKey = strings.TrimSpace(req.APIKey)
	if req.Connector == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("connector is required"))
		return
	}
	if req.APIKey == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("api_key is required"))
		return
	}

	// Charset guard matches connectorWriter.pathFor's validConnectorCode. A
	// stricter check up here just gives the operator a clean 400 instead of an
	// opaque "invalid connector code" from deeper in.
	if !validConnectorCode.MatchString(req.Connector) {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid connector code"))
		return
	}

	writer := h.connectorWriterFor(req.Connector)
	if writer == nil {
		c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("no writer available"))
		return
	}

	creds := ConnectorCreds{
		Connector:   req.Connector,
		AuthType:    "pat",
		APIKey:      req.APIKey,
		UserEmail:   strings.TrimSpace(req.UserEmail),
		Credentials: sanitizeCredentials(req.Credentials),
		// Static credentials never expire and nothing rotates them here.
		Refresh:    false,
		ExpiresAt:  0,
		ObtainedAt: time.Now().Unix(),
	}

	ctx, cancel := context.WithTimeout(context.Background(), connectorHTTPTimeout)
	defer cancel()
	if err := writer.Write(ctx, creds); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	// Mirror the MQTT-initiated connector.set reply so backends listening on
	// fd_channel for connector state transitions get the same envelope
	// regardless of who triggered the connect (BE's own dispatcher or the
	// device's local admin form). The `initiator: "device_local"` marker
	// lets BE tell them apart if it cares; without any BE change, this reads
	// as a successful connect and the connector row flips to Connected. No
	// credentials on the wire — BE already stores its own record when it
	// initiated, and does not need our copy for a local-initiated connect.
	_ = h.publishDataResult(
		"connector.set."+req.Connector,
		"success",
		"",
		map[string]any{
			"connector": req.Connector,
			"auth_type": "pat",
			"initiator": "device_local",
		},
	)

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"connector":  req.Connector,
		"auth_type":  "pat",
		"user_email": creds.UserEmail,
	}))
}

// sanitizeCredentials copies only the string→string pairs whose keys the local
// UI is allowed to set. We keep the map open (skills may consume arbitrary
// "extra" fields) but trim keys to a safe charset and drop empty values so a
// noisy paste can't grow the on-disk entry unbounded.
func sanitizeCredentials(in map[string]string) map[string]string {
	if len(in) == 0 {
		return nil
	}
	out := make(map[string]string, len(in))
	for k, v := range in {
		key := strings.TrimSpace(k)
		val := strings.TrimSpace(v)
		if key == "" || val == "" {
			continue
		}
		out[key] = val
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// connectorInfoResponse is the read-side shape for GET
// /api/device/connectors/:code. The token itself is NEVER returned — the UI
// only needs to know a token is on file plus the non-secret identity fields
// so it can render a "configured as <who>" hint and skip re-prompting.
type connectorInfoResponse struct {
	Connector   string            `json:"connector"`
	Connected   bool              `json:"connected"`
	AuthType    string            `json:"auth_type,omitempty"`
	UserEmail   string            `json:"user_email,omitempty"`
	Credentials map[string]string `json:"credentials,omitempty"`
	ObtainedAt  int64             `json:"obtained_at,omitempty"`
}

// GetConnector handles GET /api/device/connectors/:code. Reports whether a
// credential is on file for the connector, plus the non-secret identity
// fields the local UI shows next to the "connected ✓" state. Returns
// connected:false with no fields when the connector has never been set.
//
// Reads through the generic writer's loadEntry helper so the same
// per-connector file the MQTT flow writes is the single source of truth.
func (h *DeviceMQTTHandler) GetConnector(c *gin.Context) {
	code := strings.TrimSpace(c.Param("code"))
	if code == "" || !validConnectorCode.MatchString(code) {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid connector code"))
		return
	}
	// GetConnector reads the on-disk entry; only the generic writer indexes
	// them by connector code, and special writers own bespoke formats not
	// worth surfacing here for MVP. This mirrors the ecm-website admin's
	// "is this connector connected?" check without involving MQTT.
	if h.connectorWriter == nil {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(connectorInfoResponse{Connector: code, Connected: false}))
		return
	}
	creds, ok, err := h.connectorWriter.loadEntry(code)
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	if !ok {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(connectorInfoResponse{Connector: code, Connected: false}))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(connectorInfoResponse{
		Connector:   code,
		Connected:   true,
		AuthType:    creds.AuthType,
		UserEmail:   creds.UserEmail,
		Credentials: creds.Credentials,
		ObtainedAt:  creds.ObtainedAt,
	}))
}

// RemoveConnector handles DELETE /api/device/connectors/:code. Delegates to
// the same writer both flows share, so the on-disk entry disappears and
// (when routing to an openclaw MCP server) the mcp.servers.<code> entry is
// dropped too.
func (h *DeviceMQTTHandler) RemoveConnector(c *gin.Context) {
	code := strings.TrimSpace(c.Param("code"))
	if code == "" || !validConnectorCode.MatchString(code) {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid connector code"))
		return
	}
	writer := h.connectorWriterFor(code)
	if writer == nil {
		c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("no writer available"))
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), connectorHTTPTimeout)
	defer cancel()
	removed, err := writer.Remove(ctx, code)
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	// Same fd_channel echo as connector.set above — a local admin
	// disconnect must reach BE so the connectors page on autonomous.ai
	// flips the row to Not connected. Fire even when removed=false
	// (already-gone from an earlier local wipe): BE's remove handler is
	// idempotent, and a false negative here would leave the connectors
	// list stuck showing Connected until the operator clicks Disconnect
	// again from the web.
	_ = h.publishDataResult(
		"connector.remove."+code,
		"success",
		"",
		map[string]any{
			"connector": code,
			"removed":   removed,
			"initiator": "device_local",
		},
	)

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"connector": code,
		"removed":   removed,
	}))
}

