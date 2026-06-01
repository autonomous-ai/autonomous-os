package twitch

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const (
	helixBase  = "https://api.twitch.tv/helix"
	oauthToken = "https://id.twitch.tv/oauth2/token"
)

// Client is a minimal Helix client — only the calls needed to set up and
// manage a chat-message EventSub subscription.
type Client struct {
	ClientID     string
	ClientSecret string
	HTTP         *http.Client

	// Cached app access token.
	token  string
	expiry time.Time
}

func NewClient(clientID, clientSecret string) *Client {
	return &Client{
		ClientID:     clientID,
		ClientSecret: clientSecret,
		HTTP:         &http.Client{Timeout: 15 * time.Second},
	}
}

// AppAccessToken fetches (and caches) a client-credentials app token.
// Use this for webhook subscriptions that do not require a user scope on
// behalf of the broadcaster — channel.chat.message DOES need a user token
// with user:read:chat for the bot, so for production you'll likely store
// a refreshed user token instead. This helper is provided for completeness
// and for endpoints that accept an app token.
func (c *Client) AppAccessToken(ctx context.Context) (string, error) {
	if c.token != "" && time.Until(c.expiry) > 30*time.Second {
		return c.token, nil
	}
	form := url.Values{}
	form.Set("client_id", c.ClientID)
	form.Set("client_secret", c.ClientSecret)
	form.Set("grant_type", "client_credentials")

	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, oauthToken,
		strings.NewReader(form.Encode()))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return "", fmt.Errorf("app token: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("app token: %s: %s", resp.Status, string(b))
	}

	var out struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", fmt.Errorf("app token decode: %w", err)
	}
	c.token = out.AccessToken
	c.expiry = time.Now().Add(time.Duration(out.ExpiresIn) * time.Second)
	return c.token, nil
}

// SubscribeChatMessage creates a channel.chat.message EventSub subscription
// using the webhook transport.
//
//   - broadcasterUserID: numeric Twitch user ID of the channel to listen to.
//   - botUserID: numeric Twitch user ID of the bot account that will "read"
//     the chat (the user that granted user:read:chat).
//   - callback: public HTTPS URL Twitch will POST events to.
//   - secret: 10-100 char string used to sign payloads (store this — you
//     need it server-side to verify each delivery).
//   - userToken: OAuth user access token of the bot account, with at least
//     user:read:chat. App tokens are NOT enough for this subscription type.
func (c *Client) SubscribeChatMessage(
	ctx context.Context,
	broadcasterUserID, botUserID, callback, secret, userToken string,
) (Subscription, error) {
	body, _ := json.Marshal(map[string]any{
		"type":    "channel.chat.message",
		"version": "1",
		"condition": map[string]string{
			"broadcaster_user_id": broadcasterUserID,
			"user_id":             botUserID,
		},
		"transport": map[string]string{
			"method":   "webhook",
			"callback": callback,
			"secret":   secret,
		},
	})

	req, _ := http.NewRequestWithContext(ctx, http.MethodPost,
		helixBase+"/eventsub/subscriptions", bytes.NewReader(body))
	req.Header.Set("Client-Id", c.ClientID)
	req.Header.Set("Authorization", "Bearer "+userToken)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return Subscription{}, fmt.Errorf("subscribe: %w", err)
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusAccepted {
		return Subscription{}, fmt.Errorf("subscribe: %s: %s", resp.Status, string(respBody))
	}

	var out struct {
		Data []Subscription `json:"data"`
	}
	if err := json.Unmarshal(respBody, &out); err != nil {
		return Subscription{}, fmt.Errorf("subscribe decode: %w", err)
	}
	if len(out.Data) == 0 {
		return Subscription{}, fmt.Errorf("subscribe: empty response: %s", string(respBody))
	}
	return out.Data[0], nil
}

// ListSubscriptions returns all current EventSub subscriptions on this client.
// Pass an app token (cheapest) — Twitch allows this endpoint with either token type.
func (c *Client) ListSubscriptions(ctx context.Context, appToken string) ([]Subscription, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet,
		helixBase+"/eventsub/subscriptions", nil)
	req.Header.Set("Client-Id", c.ClientID)
	req.Header.Set("Authorization", "Bearer "+appToken)

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, fmt.Errorf("list: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("list: %s: %s", resp.Status, string(b))
	}
	var out struct {
		Data []Subscription `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("list decode: %w", err)
	}
	return out.Data, nil
}

// DeleteSubscription removes a subscription by ID.
func (c *Client) DeleteSubscription(ctx context.Context, appToken, id string) error {
	req, _ := http.NewRequestWithContext(ctx, http.MethodDelete,
		helixBase+"/eventsub/subscriptions?id="+url.QueryEscape(id), nil)
	req.Header.Set("Client-Id", c.ClientID)
	req.Header.Set("Authorization", "Bearer "+appToken)

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return fmt.Errorf("delete: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("delete: %s: %s", resp.Status, string(b))
	}
	return nil
}

// GetUserIDByLogin resolves a Twitch login name to its numeric user ID.
// Needs an app or user token.
func (c *Client) GetUserIDByLogin(ctx context.Context, token, login string) (string, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet,
		helixBase+"/users?login="+url.QueryEscape(login), nil)
	req.Header.Set("Client-Id", c.ClientID)
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return "", fmt.Errorf("get user: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("get user: %s: %s", resp.Status, string(b))
	}
	var out struct {
		Data []struct {
			ID    string `json:"id"`
			Login string `json:"login"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", fmt.Errorf("get user decode: %w", err)
	}
	if len(out.Data) == 0 {
		return "", fmt.Errorf("get user: login %q not found", login)
	}
	return out.Data[0].ID, nil
}
