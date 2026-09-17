package mqtthandler

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/serializers"
)

// Spotify OAuth token endpoint. Hardcoded because Spotify only accepts this
// exact URL and the mint-behind-the-button UX depends on it being reachable.
const spotifyTokenURL = "https://accounts.spotify.com/api/token"

// The redirect_uri that goes into the token exchange MUST match the one the
// authorize step used (Spotify verifies both sides). The device Settings guide
// pins this to 127.0.0.1:8888 because that is a loopback-only URI Spotify's
// OAuth policy admits without HTTPS — it does not need a real server behind
// it. See the guide in SpotifySection.tsx.
const spotifyRedirectURI = "http://127.0.0.1:8888/callback"

// The connector code we store under. Matches the ecm-website registry key
// (SPOTIFY_PAT) so both flows land on the same on-disk file.
const spotifyConnectorCode = "spotify"

// spotifyExchangeRequest is the body of POST /api/device/connectors/spotify/
// exchange-code. The Settings UI collects the three from the form (client id
// + secret already typed by the user; code pasted from the OAuth callback URL
// bar) and hands the whole exchange to the device — so the user never touches
// a terminal.
type spotifyExchangeRequest struct {
	Code         string `json:"code"`
	ClientID     string `json:"client_id"`
	ClientSecret string `json:"client_secret"`
}

// spotifyTokenResponse is the shape Spotify's token endpoint returns on
// success. We only care about refresh_token here — access_token is short-lived
// (~1h) and the skill layer mints a fresh one from refresh_token on demand.
type spotifyTokenResponse struct {
	AccessToken  string `json:"access_token"`
	TokenType    string `json:"token_type"`
	ExpiresIn    int    `json:"expires_in"`
	RefreshToken string `json:"refresh_token"`
	Scope        string `json:"scope"`
	// On failure Spotify sends { "error": "...", "error_description": "..." }
	// with status 4xx. We propagate error_description upstream so the UI can
	// show it verbatim (typical values: "Invalid authorization code",
	// "Redirect URI mismatch", "Invalid client secret").
	Error            string `json:"error,omitempty"`
	ErrorDescription string `json:"error_description,omitempty"`
}

// SpotifyExchangeCode handles POST /api/device/connectors/spotify/exchange-code.
// The client sends the OAuth authorization code (from the ?code=... URL bar
// value) plus the Client ID + Client Secret the user already typed into the
// form. The device does the token-exchange server-side (Client Secret stays
// on-box, never round-trips through the browser again) and persists via the
// same connectorWriter the PAT path uses — so the resulting on-disk file is
// indistinguishable from a manually-pasted refresh_token.
//
// Returns the refresh_token in the response body so the UI can fill it into
// the form's Refresh Token field for visual confirmation. The credential is
// ALSO already persisted at that point, so the UI can safely close the modal
// without a second Connect click.
func (h *DeviceMQTTHandler) SpotifyExchangeCode(c *gin.Context) {
	var req spotifyExchangeRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid body: "+err.Error()))
		return
	}
	req.Code = strings.TrimSpace(req.Code)
	req.ClientID = strings.TrimSpace(req.ClientID)
	req.ClientSecret = strings.TrimSpace(req.ClientSecret)
	if req.Code == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("code is required"))
		return
	}
	if req.ClientID == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("client_id is required"))
		return
	}
	if req.ClientSecret == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("client_secret is required"))
		return
	}

	// Exchange the auth code for tokens. Basic auth uses the Client ID +
	// Secret; the code + grant_type + redirect_uri go in the form body.
	form := url.Values{}
	form.Set("grant_type", "authorization_code")
	form.Set("code", req.Code)
	form.Set("redirect_uri", spotifyRedirectURI)

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, spotifyTokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("build request: "+err.Error()))
		return
	}
	httpReq.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	httpReq.SetBasicAuth(req.ClientID, req.ClientSecret)

	client := &http.Client{Timeout: 20 * time.Second}
	httpResp, err := client.Do(httpReq)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("spotify token endpoint: "+err.Error()))
		return
	}
	defer httpResp.Body.Close()

	body, err := io.ReadAll(httpResp.Body)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("read spotify response: "+err.Error()))
		return
	}

	var tok spotifyTokenResponse
	if err := json.Unmarshal(body, &tok); err != nil {
		// Non-JSON response from Spotify — surface the raw payload so the
		// operator can see what actually came back (helps diagnose a
		// transparent proxy that swallowed the JSON).
		c.JSON(http.StatusBadGateway, serializers.ResponseError(
			fmt.Sprintf("spotify returned non-json (%d): %s", httpResp.StatusCode, truncate(string(body), 200)),
		))
		return
	}

	if httpResp.StatusCode != http.StatusOK || tok.Error != "" {
		msg := tok.ErrorDescription
		if msg == "" {
			msg = tok.Error
		}
		if msg == "" {
			msg = fmt.Sprintf("spotify returned %d", httpResp.StatusCode)
		}
		c.JSON(http.StatusBadRequest, serializers.ResponseError("spotify rejected: "+msg))
		return
	}

	if tok.RefreshToken == "" {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("spotify response missing refresh_token"))
		return
	}
	if !strings.Contains(tok.Scope, "streaming") {
		// Not fatal — the skill layer can still search, list playlists, etc.
		// But playback commands will 403 with PREMIUM_REQUIRED. Warn loud so
		// the UI can surface it before the user tries to play music.
		slog.Warn("spotify refresh_token missing 'streaming' scope",
			"component", "spotify_oauth",
			"granted_scopes", tok.Scope)
	}

	// Persist the same way the PAT path does. The refresh_token becomes the
	// api_key slot (long-lived durable credential); Client ID + Secret ride
	// in the credentials map so the skill can mint fresh access tokens
	// without user involvement.
	writer := h.connectorWriterFor(spotifyConnectorCode)
	if writer == nil {
		c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("no writer available"))
		return
	}
	creds := ConnectorCreds{
		Connector: spotifyConnectorCode,
		AuthType:  "pat",
		APIKey:    tok.RefreshToken,
		Credentials: sanitizeCredentials(map[string]string{
			"client_id":     req.ClientID,
			"client_secret": req.ClientSecret,
			"scope":         tok.Scope,
		}),
		Refresh:    false,
		ExpiresAt:  0,
		ObtainedAt: time.Now().Unix(),
	}

	writeCtx, writeCancel := context.WithTimeout(context.Background(), connectorHTTPTimeout)
	defer writeCancel()
	if err := writer.Write(writeCtx, creds); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("persist credential: "+err.Error()))
		return
	}

	// Same fd_channel emit as the PAT path, so BE observes the connect the
	// same way regardless of who triggered it. `initiator: "device_local"`
	// keeps that distinction in the payload.
	_ = h.publishDataResult(
		"connector.set."+spotifyConnectorCode,
		"success",
		"",
		map[string]any{
			"connector": spotifyConnectorCode,
			"auth_type": "pat",
			"initiator": "device_local",
		},
	)

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"connector":     spotifyConnectorCode,
		"auth_type":     "pat",
		"refresh_token": tok.RefreshToken,
		"scope":         tok.Scope,
	}))
}

// truncate clips a string to at most n runes, adding an ellipsis if it was
// clipped. Used for the "Spotify returned non-json" fallback so we don't
// dump a wall of HTML into the UI toast.
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
