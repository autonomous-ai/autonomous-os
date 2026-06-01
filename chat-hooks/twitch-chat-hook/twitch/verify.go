package twitch

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// EventSub webhook headers.
// https://dev.twitch.tv/docs/eventsub/handling-webhook-events/#verifying-the-event-message
const (
	HeaderMessageID        = "Twitch-Eventsub-Message-Id"
	HeaderMessageTimestamp = "Twitch-Eventsub-Message-Timestamp"
	HeaderMessageSignature = "Twitch-Eventsub-Message-Signature"
	HeaderMessageType      = "Twitch-Eventsub-Message-Type"
	HeaderMessageRetry     = "Twitch-Eventsub-Message-Retry"
	HeaderSubscriptionType = "Twitch-Eventsub-Subscription-Type"
)

// MaxAge is the maximum accepted skew between the Twitch timestamp and now.
// Twitch recommends rejecting anything older than 10 minutes to mitigate replay.
const MaxAge = 10 * time.Minute

var (
	ErrMissingHeaders   = errors.New("twitch: missing signature headers")
	ErrTimestampInvalid = errors.New("twitch: invalid timestamp")
	ErrTimestampStale   = errors.New("twitch: timestamp outside allowed window")
	ErrBadSignature     = errors.New("twitch: signature mismatch")
)

// VerifySignature checks the HMAC-SHA256 signature Twitch sends with each
// webhook delivery. body must be the exact raw request body.
//
// Returns nil on success.
func VerifySignature(h http.Header, body, secret []byte) error {
	id := h.Get(HeaderMessageID)
	ts := h.Get(HeaderMessageTimestamp)
	sig := h.Get(HeaderMessageSignature)
	if id == "" || ts == "" || sig == "" {
		return ErrMissingHeaders
	}

	parsed, err := time.Parse(time.RFC3339Nano, ts)
	if err != nil {
		return ErrTimestampInvalid
	}
	if abs(time.Since(parsed)) > MaxAge {
		return ErrTimestampStale
	}

	const prefix = "sha256="
	if !strings.HasPrefix(sig, prefix) {
		return ErrBadSignature
	}
	expectedHex := strings.TrimPrefix(sig, prefix)
	expected, err := hex.DecodeString(expectedHex)
	if err != nil {
		return ErrBadSignature
	}

	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(id))
	mac.Write([]byte(ts))
	mac.Write(body)
	got := mac.Sum(nil)

	if !hmac.Equal(got, expected) {
		return ErrBadSignature
	}
	return nil
}

// RetryCount returns the retry attempt for this delivery (0 = first try).
func RetryCount(h http.Header) int {
	n, _ := strconv.Atoi(h.Get(HeaderMessageRetry))
	return n
}

func abs(d time.Duration) time.Duration {
	if d < 0 {
		return -d
	}
	return d
}
