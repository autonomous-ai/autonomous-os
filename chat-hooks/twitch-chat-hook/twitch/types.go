package twitch

import (
	"encoding/json"
	"time"
)

// MessageType values sent in the Twitch-Eventsub-Message-Type header.
const (
	MsgTypeVerification = "webhook_callback_verification"
	MsgTypeNotification = "notification"
	MsgTypeRevocation   = "revocation"
)

// EventSub notification envelope. Same shape for all subscription types;
// the actual event lives in Event as raw JSON so callers can decode it
// into the type that matches Subscription.Type.
type Envelope struct {
	Subscription Subscription    `json:"subscription"`
	Challenge    string          `json:"challenge,omitempty"` // verification only
	Event        json.RawMessage `json:"event,omitempty"`
}

type Subscription struct {
	ID        string         `json:"id"`
	Status    string         `json:"status"`
	Type      string         `json:"type"`
	Version   string         `json:"version"`
	Cost      int            `json:"cost"`
	Condition map[string]any `json:"condition"`
	Transport Transport      `json:"transport"`
	CreatedAt time.Time      `json:"created_at"`
}

type Transport struct {
	Method   string `json:"method"`
	Callback string `json:"callback,omitempty"`
}

// ChatMessageEvent matches subscription type "channel.chat.message" v1.
// https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/#channelchatmessage
type ChatMessageEvent struct {
	BroadcasterUserID    string `json:"broadcaster_user_id"`
	BroadcasterUserLogin string `json:"broadcaster_user_login"`
	BroadcasterUserName  string `json:"broadcaster_user_name"`

	ChatterUserID    string `json:"chatter_user_id"`
	ChatterUserLogin string `json:"chatter_user_login"`
	ChatterUserName  string `json:"chatter_user_name"`

	MessageID string  `json:"message_id"`
	Message   Message `json:"message"`

	// One of: text, channel_points_highlighted, channel_points_sub_only,
	// user_intro, power_ups_message_effect, power_ups_gigantified_emote.
	MessageType string `json:"message_type"`

	Color  string  `json:"color"`
	Badges []Badge `json:"badges"`

	Cheer      *Cheer  `json:"cheer,omitempty"`
	Reply      *Reply  `json:"reply,omitempty"`
	ChannelPts *string `json:"channel_points_custom_reward_id,omitempty"`
}

type Message struct {
	Text      string     `json:"text"`
	Fragments []Fragment `json:"fragments"`
}

// Fragment.Type: text | cheermote | emote | mention.
type Fragment struct {
	Type      string     `json:"type"`
	Text      string     `json:"text"`
	Cheermote *Cheermote `json:"cheermote,omitempty"`
	Emote     *Emote     `json:"emote,omitempty"`
	Mention   *Mention   `json:"mention,omitempty"`
}

type Cheermote struct {
	Prefix string `json:"prefix"`
	Bits   int    `json:"bits"`
	Tier   int    `json:"tier"`
}

type Emote struct {
	ID         string   `json:"id"`
	EmoteSetID string   `json:"emote_set_id"`
	OwnerID    string   `json:"owner_id"`
	Format     []string `json:"format"`
}

type Mention struct {
	UserID    string `json:"user_id"`
	UserLogin string `json:"user_login"`
	UserName  string `json:"user_name"`
}

type Badge struct {
	SetID string `json:"set_id"`
	ID    string `json:"id"`
	Info  string `json:"info"`
}

type Cheer struct {
	Bits int `json:"bits"`
}

type Reply struct {
	ParentMessageID   string `json:"parent_message_id"`
	ParentMessageBody string `json:"parent_message_body"`
	ParentUserID      string `json:"parent_user_id"`
	ParentUserLogin   string `json:"parent_user_login"`
	ParentUserName    string `json:"parent_user_name"`
	ThreadMessageID   string `json:"thread_message_id"`
	ThreadUserID      string `json:"thread_user_id"`
	ThreadUserLogin   string `json:"thread_user_login"`
	ThreadUserName    string `json:"thread_user_name"`
}
