package http

import (
	"log/slog"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
)

// upgrader configures the WS upgrade. CheckOrigin is permissive because the
// buddy is a native macOS app, not a browser; bearer auth in the handler is the
// real gate.
var upgrader = websocket.Upgrader{
	ReadBufferSize:  4096,
	WriteBufferSize: 4096,
	CheckOrigin:     func(r *http.Request) bool { return true },
	// Negotiate permessage-deflate when the client asks for it. macOS Ventura's
	// URLSessionWebSocketTask requests this extension and on some builds will
	// keep treating frames as compressed even if the server ignores the request
	// — agreeing here keeps both sides in sync.
	EnableCompression: false,
}

// WS upgrades to WebSocket after validating the Bearer token against the
// stored pairing. Once connected, ownership of the read loop transfers to the
// buddy service, which routes incoming responses to pending Dispatch callers.
func (h *BuddyHandler) WS(c *gin.Context) {
	auth := c.GetHeader("Authorization")
	if !strings.HasPrefix(auth, "Bearer ") {
		c.String(http.StatusUnauthorized, "missing bearer")
		return
	}
	token := strings.TrimPrefix(auth, "Bearer ")
	record := h.service.ValidateToken(token)
	if record == nil {
		c.String(http.StatusUnauthorized, "invalid token")
		return
	}
	conn, err := upgrader.Upgrade(c.Writer, c.Request, nil)
	if err != nil {
		slog.Warn("WS upgrade failed", "component", "buddy", "error", err)
		return
	}
	h.service.RegisterConnection(conn)
	// Fire a hello ping in the background so the buddy app's Activity window
	// gets one immediate ✓ row — confirms end-to-end reachability the moment
	// pairing completes. Must run in a goroutine: Dispatch blocks until the
	// buddy responds, which can only happen after RunReadLoop below starts
	// pumping the WS.
	go h.service.Greet(record.BuddyID)
	// Block on the read loop in this request goroutine. Gin will keep the
	// HTTP request "alive" until this returns, which is what we want for a
	// long-lived WS.
	h.service.RunReadLoop(conn, record.BuddyID)
}
