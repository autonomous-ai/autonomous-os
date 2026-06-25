import { C, LockedField, SectionCard } from "@/components/setup/shared";
import { SecretUpdateField } from "@/components/SecretUpdateField";
import type { ChannelType } from "@/types";
import type { ChannelLoadedState } from "@/hooks/setup/types";

// Bot tokens now go through SecretUpdateField (write-only) — the server only
// returns has_* booleans so the previous "show saved token" affordance is
// gone. Channel IDs stay plain LockedField since they're not secrets.
export function ChannelSection({
  active, channel, setChannel, channelLoaded,
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
    <SectionCard id="channel" title="Messaging Channels" active={active}>
      <div style={{ marginBottom: 12 }}>
        <label style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>Channel</label>
        <select
          value={channel}
          onChange={(e) => setChannel(e.target.value as ChannelType)}
          style={{
            width: "100%", boxSizing: "border-box" as const,
            background: C.surface, border: `1px solid ${C.border}`,
            borderRadius: 7, padding: "8px 11px",
            fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
          }}
        >
          <option value="telegram">Telegram</option>
          <option value="slack">Slack</option>
          <option value="discord">Discord</option>
        </select>
      </div>
      {channel === "telegram" && (
        <>
          <SecretUpdateField configured={channelLoaded.teleToken} label="Bot Token" id="tele_token" value={teleToken} onChange={setTeleToken} placeholder="123456:ABC-DEF..." />
          <LockedField lockedInitially={channelLoaded.teleUserId} label="User ID" id="tele_user_id" value={teleUserId} onChange={setTeleUserId} placeholder="123456789" />
        </>
      )}
      {channel === "slack" && (
        <>
          <SecretUpdateField configured={channelLoaded.slackBotToken} label="Bot Token" id="slack_bot_token" value={slackBotToken} onChange={setSlackBotToken} placeholder="xoxb-..." />
          <SecretUpdateField configured={channelLoaded.slackAppToken} label="App Token" id="slack_app_token" value={slackAppToken} onChange={setSlackAppToken} placeholder="xapp-..." />
          <LockedField lockedInitially={channelLoaded.slackUserId} label="User ID" id="slack_user_id" value={slackUserId} onChange={setSlackUserId} placeholder="U0123456789" />
        </>
      )}
      {channel === "discord" && (
        <>
          <SecretUpdateField configured={channelLoaded.discordBotToken} label="Bot Token" id="discord_bot_token" value={discordBotToken} onChange={setDiscordBotToken} placeholder="Bot token" />
          <LockedField lockedInitially={channelLoaded.discordGuildId} label="Guild ID" id="discord_guild_id" value={discordGuildId} onChange={setDiscordGuildId} placeholder="123456789" />
          <LockedField lockedInitially={channelLoaded.discordUserId} label="User ID" id="discord_user_id" value={discordUserId} onChange={setDiscordUserId} placeholder="123456789" />
        </>
      )}
    </SectionCard>
  );
}
