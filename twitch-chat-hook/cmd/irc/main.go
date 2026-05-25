// Anonymous IRC reader for Twitch chat. No app, no token, no 2FA required.
//
// Twitch keeps a read-only IRC gateway at irc.chat.twitch.tv:6697 (TLS).
// Anyone can connect with a "justinfan<digits>" nick and JOIN any public
// channel — no PASS, no OAuth.
//
// This is a stand-in for the EventSub webhook path (cmd/webhook) while the
// Twitch Developer Console is unreachable. Output format matches webhook's
// handleChatMessage so downstream code sees the same shape.
//
// Usage:
//   go run ./cmd/irc -channel <broadcaster_login>
//
// Notes:
//   - Twitch has announced IRC deprecation; OK for short-term, migrate to
//     EventSub for production.
//   - Single channel for now. Pass comma-separated logins to -channel for
//     multi-channel join.

package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"twitch-chat-hook/twitch"
)

const twitchIRCHost = "irc.chat.twitch.tv:6697"

// Version is injected at build time via -ldflags "-X main.Version=...".
var Version = "dev"

func main() {
	channels := flag.String("channel", "", "Twitch channel login(s), comma-separated, no leading #")
	flag.Parse()

	if *channels == "" {
		flag.Usage()
		os.Exit(2)
	}

	log.Printf("[twitch-irc] version=%s", Version)

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	nick := fmt.Sprintf("justinfan%d", 10000+rand.Intn(89999))
	chs := splitChannels(*channels)

	if err := run(ctx, chs, nick); err != nil && !errors.Is(err, context.Canceled) {
		log.Fatalf("[twitch-irc] %v", err)
	}
}

func run(ctx context.Context, channels []string, nick string) error {
	log.Printf("[twitch-irc] connecting as %s, joining %s", nick, strings.Join(channels, ", "))

	d := &tls.Dialer{Config: &tls.Config{}}
	conn, err := d.DialContext(ctx, "tcp", twitchIRCHost)
	if err != nil {
		return fmt.Errorf("dial: %w", err)
	}
	defer conn.Close()

	// Close connection when context is cancelled so the scanner unblocks.
	go func() {
		<-ctx.Done()
		_ = conn.Close()
	}()

	if _, err := fmt.Fprintf(conn, "NICK %s\r\n", nick); err != nil {
		return fmt.Errorf("nick: %w", err)
	}
	for _, ch := range channels {
		if _, err := fmt.Fprintf(conn, "JOIN #%s\r\n", ch); err != nil {
			return fmt.Errorf("join: %w", err)
		}
	}

	scanner := bufio.NewScanner(conn)
	scanner.Buffer(make([]byte, 0, 64*1024), 512*1024)

	for scanner.Scan() {
		line := strings.TrimRight(scanner.Text(), "\r\n")
		if line == "" {
			continue
		}
		handleLine(ctx, conn, line)
	}
	if err := scanner.Err(); err != nil {
		if errors.Is(err, net.ErrClosed) || errors.Is(err, io.EOF) {
			return ctx.Err()
		}
		return fmt.Errorf("read: %w", err)
	}
	return ctx.Err()
}

func handleLine(ctx context.Context, w io.Writer, line string) {
	// PING from server — must PONG within ~5 min or the connection is dropped.
	if strings.HasPrefix(line, "PING ") {
		payload := strings.TrimPrefix(line, "PING ")
		_, _ = fmt.Fprintf(w, "PONG %s\r\n", payload)
		return
	}

	if msg, ok := parsePrivmsg(line); ok {
		log.Printf("[twitch-chat] #%s <%s> %s", msg.channel, msg.nick, msg.text)
		twitch.ForwardChatMessage(ctx, msg.nick, msg.text)
		return
	}

	// Surface notices and join confirmation so the user knows the link is up.
	if strings.Contains(line, " 001 ") || strings.Contains(line, " JOIN ") || strings.Contains(line, " NOTICE ") {
		log.Printf("[twitch-irc] %s", line)
	}
}

type privmsg struct {
	nick    string
	channel string
	text    string
}

// parsePrivmsg parses a Twitch IRC line of the form:
//
//	[@tags ] :nick!user@host PRIVMSG #channel :message text
//
// IRCv3 tags prefix is tolerated but unused.
func parsePrivmsg(line string) (privmsg, bool) {
	if strings.HasPrefix(line, "@") {
		sp := strings.IndexByte(line, ' ')
		if sp < 0 {
			return privmsg{}, false
		}
		line = line[sp+1:]
	}
	if !strings.HasPrefix(line, ":") {
		return privmsg{}, false
	}
	sp := strings.IndexByte(line, ' ')
	if sp < 0 {
		return privmsg{}, false
	}
	prefix := line[1:sp]
	rest := line[sp+1:]

	sp2 := strings.IndexByte(rest, ' ')
	if sp2 < 0 {
		return privmsg{}, false
	}
	cmd := rest[:sp2]
	if cmd != "PRIVMSG" {
		return privmsg{}, false
	}
	rest = rest[sp2+1:]

	sp3 := strings.Index(rest, " :")
	if sp3 < 0 {
		return privmsg{}, false
	}
	channel := strings.TrimPrefix(rest[:sp3], "#")
	text := rest[sp3+2:]

	nick := prefix
	if bang := strings.IndexByte(nick, '!'); bang >= 0 {
		nick = nick[:bang]
	}

	return privmsg{nick: nick, channel: channel, text: text}, true
}

func splitChannels(csv string) []string {
	parts := strings.Split(csv, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(strings.TrimPrefix(strings.ToLower(p), "#"))
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}
