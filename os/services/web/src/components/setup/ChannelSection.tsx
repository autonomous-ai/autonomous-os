import { MessageSquare } from "lucide-react";
import { ConfiguredHint, LockedField, LockedPasswordField, SectionCard, LABEL_STYLE, INPUT_STYLE, FIELD_GAP } from "./shared";
import type { ChannelType } from "@/types";
import type { ChannelLoadedState } from "@/hooks/setup/types";

export function ChannelSection({
  active, channel, setChannel,
  channelLoaded,
  teleToken, setTeleToken, teleUserId, setTeleUserId,
  slackBotToken, setSlackBotToken, slackAppToken, setSlackAppToken, slackUserId, setSlackUserId,
  discordBotToken, setDiscordBotToken, discordGuildId, setDiscordGuildId, discordUserId, setDiscordUserId,
}: {
  active: boolean;
  channel: ChannelType;
  setChannel: (v: ChannelType) => void;
  channelLoaded: ChannelLoadedState;
  teleToken: string; setTeleToken: (v: string) => void;
  teleUserId: string; setTeleUserId: (v: string) => void;
  slackBotToken: string; setSlackBotToken: (v: string) => void;
  slackAppToken: string; setSlackAppToken: (v: string) => void;
  slackUserId: string; setSlackUserId: (v: string) => void;
  discordBotToken: string; setDiscordBotToken: (v: string) => void;
  discordGuildId: string; setDiscordGuildId: (v: string) => void;
  discordUserId: string; setDiscordUserId: (v: string) => void;
}) {
  return (
    <SectionCard id="channel" title="Messaging Channels" active={active} icon={<MessageSquare size={17} />}
      description="Connect a chat app so you can message your device and get its replies.">
      <div style={{ marginBottom: FIELD_GAP }}>
        <label htmlFor="channel" style={LABEL_STYLE}>Channel *</label>
        <select
          id="channel"
          value={channel}
          onChange={(e) => setChannel(e.target.value as ChannelType)}
          style={{ ...INPUT_STYLE, cursor: "pointer" }}
        >
          <option value="telegram">Telegram</option>
          <option value="slack">Slack</option>
          <option value="discord">Discord</option>
        </select>
      </div>
      {channel === "telegram" && (
        <>
          {channelLoaded.teleToken ? (
            <ConfiguredHint label="Bot Token" />
          ) : (
            <LockedPasswordField required lockedInitially={false} label="Bot Token *" id="tele_token" value={teleToken} onChange={setTeleToken} placeholder="123456:ABC-DEF..." />
          )}
          <LockedField required lockedInitially={channelLoaded.teleUserId} label="User ID *" id="tele_user_id" value={teleUserId} onChange={setTeleUserId} placeholder="123456789" />
        </>
      )}
      {channel === "slack" && (
        <>
          {channelLoaded.slackBotToken ? (
            <ConfiguredHint label="Bot Token" />
          ) : (
            <LockedPasswordField required lockedInitially={false} label="Bot Token *" id="slack_bot_token" value={slackBotToken} onChange={setSlackBotToken} placeholder="xoxb-..." />
          )}
          {channelLoaded.slackAppToken ? (
            <ConfiguredHint label="App Token" />
          ) : (
            <LockedPasswordField required lockedInitially={false} label="App Token *" id="slack_app_token" value={slackAppToken} onChange={setSlackAppToken} placeholder="xapp-..." />
          )}
          <LockedField required lockedInitially={channelLoaded.slackUserId} label="User ID *" id="slack_user_id" value={slackUserId} onChange={setSlackUserId} placeholder="U0123456789" />
        </>
      )}
      {channel === "discord" && (
        <>
          {channelLoaded.discordBotToken ? (
            <ConfiguredHint label="Bot Token" />
          ) : (
            <LockedPasswordField required lockedInitially={false} label="Bot Token *" id="discord_bot_token" value={discordBotToken} onChange={setDiscordBotToken} placeholder="Bot token" />
          )}
          <LockedField required lockedInitially={channelLoaded.discordGuildId} label="Guild ID *" id="discord_guild_id" value={discordGuildId} onChange={setDiscordGuildId} placeholder="123456789" />
          <LockedField required lockedInitially={channelLoaded.discordUserId} label="User ID *" id="discord_user_id" value={discordUserId} onChange={setDiscordUserId} placeholder="123456789" />
        </>
      )}
    </SectionCard>
  );
}
