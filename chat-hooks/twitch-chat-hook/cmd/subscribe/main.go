// One-shot CLI to create a channel.chat.message EventSub subscription.
//
// Usage:
//   go run ./cmd/subscribe \
//     -channel  <broadcaster_login> \
//     -bot      <bot_login> \
//     -callback https://your.host/twitch/webhook
//
// Env:
//   TWITCH_CLIENT_ID
//   TWITCH_CLIENT_SECRET
//   TWITCH_WEBHOOK_SECRET    same value the webhook server uses
//   TWITCH_BOT_USER_TOKEN    OAuth user token of the bot with user:read:chat
//
// Notes:
//   - channel.chat.message REQUIRES a user access token (the bot's), not an
//     app token. Get one via OAuth Authorization Code or Device Code flow.
//   - The bot user must also have one of: channel:bot scope from broadcaster,
//     OR be a moderator in the channel.

package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"time"

	"twitch-chat-hook/twitch"
)

func main() {
	channel := flag.String("channel", "", "broadcaster login (channel to listen to)")
	bot := flag.String("bot", "", "bot login (the user reading chat)")
	callback := flag.String("callback", "", "public HTTPS callback URL")
	flag.Parse()

	if *channel == "" || *bot == "" || *callback == "" {
		flag.Usage()
		os.Exit(2)
	}

	clientID := mustEnv("TWITCH_CLIENT_ID")
	clientSecret := mustEnv("TWITCH_CLIENT_SECRET")
	hookSecret := mustEnv("TWITCH_WEBHOOK_SECRET")
	userToken := mustEnv("TWITCH_BOT_USER_TOKEN")

	if len(hookSecret) < 10 || len(hookSecret) > 100 {
		log.Fatal("TWITCH_WEBHOOK_SECRET must be 10-100 chars")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	c := twitch.NewClient(clientID, clientSecret)

	// Resolve logins -> numeric user IDs (subscription condition needs IDs).
	// Use the user token; app token also works here.
	channelID, err := c.GetUserIDByLogin(ctx, userToken, *channel)
	if err != nil {
		log.Fatalf("resolve channel: %v", err)
	}
	botID, err := c.GetUserIDByLogin(ctx, userToken, *bot)
	if err != nil {
		log.Fatalf("resolve bot: %v", err)
	}

	sub, err := c.SubscribeChatMessage(ctx, channelID, botID, *callback, hookSecret, userToken)
	if err != nil {
		log.Fatalf("subscribe: %v", err)
	}

	fmt.Printf("subscribed: id=%s status=%s type=%s\n", sub.ID, sub.Status, sub.Type)
	fmt.Println("Twitch will now POST a webhook_callback_verification to:", *callback)
	fmt.Println("Your webhook server must echo the `challenge` field for the sub to activate.")
}

func mustEnv(k string) string {
	v := os.Getenv(k)
	if v == "" {
		log.Fatalf("%s is required", k)
	}
	return v
}
