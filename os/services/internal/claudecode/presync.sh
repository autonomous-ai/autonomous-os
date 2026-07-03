#!/usr/bin/env bash
# runtime-claudecode-presync — run by switch-runtime right before claudecode
# starts, once at the end of install.sh, and by EnsureOnboarding on every
# os-server boot / config change (hermes-style). It OWNS everything stateful:
#
#   §0 BRIDGE   — materialize /root/.claudecode/bridge.py (always overwritten,
#      so a plain os-server OTA refreshes the bridge on disk — unlike a copy
#      written by install.sh, which only re-runs on a first install / failed
#      verify). The bridge holds the headless Claude Code process and exposes
#      the WebSocket os-server connects to (internal/claudecode/constants.go).
#   §1 SEEDS    — headless flags in /root/.claude.json (skip interactive
#      onboarding) + workspace .claude/settings.json (trust .mcp.json entries).
#   §2 ENV      — /root/.claudecode/.env. Auth mode is decided here: claude.ai
#      SUBSCRIPTION (claude_code_oauth_token from the login flow, or
#      ~/.claude/.credentials.json on disk → CLAUDE_CODE_OAUTH_TOKEN, no
#      ANTHROPIC_*) vs API-KEY (ANTHROPIC_* from llm_api_key / llm_base_url /
#      llm_model — the same source hermes/picoclaw presync reads).
#   §3 CHANNELS — Claude Code's native channel plugin config (telegram +
#      discord): ~/.claude/channels/<ch>/.env (bot token) + access.json
#      (dmPolicy allowlist seeded from the owner's user id — replaces the
#      interactive /telegram:access | /discord:access pairing, which a headless
#      device cannot run), and the CLAUDECODE_CHANNELS launch flag the bridge
#      passes to `claude --channels`.
#
# This file is EMBEDDED IN os-server (internal/claudecode/presync.sh) and
# materialized to /usr/local/bin/runtime-claudecode-presync on every switch.
set -euo pipefail

CONFIG_JSON="/root/config/config.json"          # device/project config (source of truth)
CC_DIR="/root/.claudecode"
WS_DIR="$CC_DIR/workspace"
ENV_FILE="$CC_DIR/.env"
BRIDGE="$CC_DIR/bridge.py"
CLAUDE_HOME="/root/.claude"

# Claude Code calls {ANTHROPIC_BASE_URL}/v1/messages — same anthropic-messages
# endpoint hermes uses, so the base has NO trailing /v1 (unlike picoclaw's
# OpenAI-style base).
DEFAULT_BASE_URL="https://campaign-api.autonomous.ai/api/v1/ai"
DEFAULT_MODEL="Auto-AI"

log() { echo "[claudecode-presync] $*"; }

command -v jq >/dev/null 2>&1 || { log "ERROR: jq not found — cannot sync claudecode config" >&2; exit 1; }

mkdir -p "$WS_DIR/.claude/skills" "$WS_DIR/memory"

# read a field from the device config.json ("" when absent/empty).
dev() { jq -r ".${1} // empty" "$CONFIG_JSON" 2>/dev/null || true; }
# jq has no in-place flag; edit via temp + rename.
jq_edit() { local f="$1"; shift; local tmp; tmp="$(mktemp)"; jq "$@" "$f" >"$tmp" && mv "$tmp" "$f"; }

# ── §0 BRIDGE (always overwrite — OTA-refreshed via this hook) ──────────────────
# TOKEN + port MUST equal internal/claudecode/constants.go (WSURL / Token).
log "materialize bridge.py"
cat >"$BRIDGE.tmp" <<'PYEOF'
#!/usr/bin/env python3
"""claudecode bridge — materialized by runtime-claudecode-presync (embedded in
os-server; do NOT edit on-device, changes are overwritten on every presync run).

Holds ONE persistent headless Claude Code process and exposes a WebSocket for
os-server (internal/claudecode):

  os-server -> bridge: {"type":"message.send","id":..,"payload":{"content":..,
                        "attachments":[{"type":"image","url":"data:<mt>;base64,<b64>"}]}}
                       {"type":"session.new"}  -> restart Claude without --resume
                       {"type":"ping",..}      -> {"type":"pong"}
  bridge -> os-server: Claude stream-json events forwarded VERBATIM
                       (system/assistant/user/result), plus {"type":"pong"} and
                       {"type":"bridge.status"|"bridge.error","payload":{..}}.

The child is `claude --print --input-format stream-json --output-format
stream-json --dangerously-skip-permissions` run in the workspace, resumed via
--resume across restarts, with `--channels <CLAUDECODE_CHANNELS>` when presync
configured a channel plugin. Env comes from /root/.claudecode/.env.
"""
import asyncio
import base64
import json
import os
import sys

try:
    import websockets
except ImportError:
    print("[claudecode-bridge] FATAL: python websockets library missing "
          "(install.sh installs python3-websockets)", flush=True)
    sys.exit(1)

HOME = "/root"
CC_DIR = "/root/.claudecode"
WORKSPACE = os.path.join(CC_DIR, "workspace")
ENV_FILE = os.path.join(CC_DIR, ".env")
SESSION_FILE = os.path.join(CC_DIR, "session.json")
HOST = "127.0.0.1"
PORT = 18791                              # constants.go WSURL
TOKEN = "autonomous_claudecode_token"     # constants.go Token
RESTART_BACKOFF = 5
STREAM_LIMIT = 8 * 1024 * 1024


def log(*args):
    print("[claudecode-bridge]", *args, flush=True)


def load_env():
    env = dict(os.environ)
    env["HOME"] = HOME
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    except FileNotFoundError:
        pass
    return env


class Bridge:
    def __init__(self):
        self.clients = set()
        self.child = None
        self.session_id = self._load_session()
        self.resume_next = bool(self.session_id)
        self.pending = []   # stdin lines queued while the child is down
        self.inflight = 0   # message.send frames not yet answered by a result

    def _load_session(self):
        try:
            with open(SESSION_FILE) as f:
                return json.load(f).get("session_id") or None
        except Exception:
            return None

    def _save_session(self):
        try:
            with open(SESSION_FILE, "w") as f:
                json.dump({"session_id": self.session_id}, f)
        except Exception as e:
            log("save session failed:", e)

    async def broadcast(self, text):
        for ws in list(self.clients):
            try:
                await ws.send(text)
            except Exception:
                self.clients.discard(ws)

    async def status(self, **extra):
        payload = {"claude_running": self.child is not None,
                   "session_id": self.session_id}
        payload.update(extra)
        await self.broadcast(json.dumps({"type": "bridge.status", "payload": payload}))

    # -- claude child ----------------------------------------------------------

    def _build_cmd(self, env):
        cmd = ["claude", "--print", "--verbose",
               "--input-format", "stream-json",
               "--output-format", "stream-json",
               "--dangerously-skip-permissions"]
        if self.resume_next and self.session_id:
            cmd += ["--resume", self.session_id]
        channels = env.get("CLAUDECODE_CHANNELS", "").split()
        if channels:
            cmd += ["--channels"] + channels
        return cmd

    async def run_forever(self):
        while True:
            env = load_env()
            cmd = self._build_cmd(env)
            log("spawning:", " ".join(cmd))
            try:
                self.child = await asyncio.create_subprocess_exec(
                    *cmd, cwd=WORKSPACE, env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=STREAM_LIMIT)
            except FileNotFoundError:
                log("claude binary not found — retry in", RESTART_BACKOFF)
                await asyncio.sleep(RESTART_BACKOFF)
                continue
            self.resume_next = True
            await self.status()
            for line in self.pending:
                self._write_stdin(line)
            self.pending = []
            await asyncio.gather(self._pump_stdout(), self._pump_stderr())
            rc = await self.child.wait()
            self.child = None
            log("claude exited rc =", rc)
            if self.inflight > 0:
                self.inflight = 0
                await self.broadcast(json.dumps({
                    "type": "bridge.error",
                    "payload": {"message": "claude exited (rc=%s) mid-turn" % rc}}))
            await self.status(exit_code=rc)
            await asyncio.sleep(RESTART_BACKOFF)

    async def _pump_stdout(self):
        while True:
            line = await self.child.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                evt = json.loads(text)
            except ValueError:
                log("non-JSON stdout:", text[:200])
                continue
            sid = evt.get("session_id")
            if sid and sid != self.session_id:
                self.session_id = sid
                self._save_session()
            if evt.get("type") == "result" and self.inflight > 0:
                self.inflight -= 1
            await self.broadcast(text)

    async def _pump_stderr(self):
        while True:
            line = await self.child.stderr.readline()
            if not line:
                return
            log("claude:", line.decode("utf-8", "replace").rstrip())

    def _write_stdin(self, line):
        if self.child is None or self.child.stdin is None:
            self.pending.append(line)
            return
        try:
            self.child.stdin.write(line.encode("utf-8") + b"\n")
        except Exception as e:
            log("stdin write failed:", e)
            self.pending.append(line)

    # -- websocket server ------------------------------------------------------

    def _authorized(self, ws):
        try:
            headers = getattr(ws, "request_headers", None)
            if headers is None:  # websockets >= 11 renamed the attribute
                headers = ws.request.headers
            return headers.get("Authorization", "") == "Bearer " + TOKEN
        except Exception:
            return False

    async def handler(self, ws, path=None):  # path arg: websockets < 11 compat
        if not self._authorized(ws):
            await ws.close(4401, "unauthorized")
            return
        self.clients.add(ws)
        log("client connected (%d total)" % len(self.clients))
        try:
            await self.status()
            async for raw in ws:
                await self._handle_frame(ws, raw)
        except Exception as e:
            log("client error:", e)
        finally:
            self.clients.discard(ws)
            log("client disconnected (%d total)" % len(self.clients))

    async def _handle_frame(self, ws, raw):
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        ftype = frame.get("type")
        if ftype == "ping":
            await ws.send(json.dumps({"type": "pong"}))
        elif ftype == "message.send":
            self._send_user_message(frame.get("payload") or {})
        elif ftype == "session.new":
            await self._new_session()

    def _send_user_message(self, payload):
        blocks = [{"type": "text", "text": payload.get("content") or ""}]
        for att in payload.get("attachments") or []:
            url = att.get("url", "")
            if not url.startswith("data:"):
                continue
            try:
                meta, b64 = url.split(",", 1)
                media = meta[5:].split(";")[0] or "image/jpeg"
                base64.b64decode(b64, validate=True)
            except Exception:
                log("bad image attachment, skipped")
                continue
            blocks.append({"type": "image",
                           "source": {"type": "base64", "media_type": media, "data": b64}})
        msg = {"type": "user", "message": {"role": "user", "content": blocks}}
        self.inflight += 1
        self._write_stdin(json.dumps(msg))

    async def _new_session(self):
        log("session.new — restarting claude without resume")
        self.session_id = None
        self.resume_next = False
        try:
            os.remove(SESSION_FILE)
        except FileNotFoundError:
            pass
        if self.child is not None:
            try:
                self.child.terminate()
            except ProcessLookupError:
                pass


async def main():
    os.makedirs(WORKSPACE, exist_ok=True)
    bridge = Bridge()
    async with websockets.serve(bridge.handler, HOST, PORT, max_size=STREAM_LIMIT):
        log("listening on ws://%s:%d/claude/ws/" % (HOST, PORT))
        await bridge.run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
PYEOF
chmod 0755 "$BRIDGE.tmp"
mv "$BRIDGE.tmp" "$BRIDGE"

# ── §1 SEEDS (headless flags, idempotent) ───────────────────────────────────────
# ~/.claude.json: skip the interactive first-run onboarding + accept the
# bypass-permissions warning — a headless device has no TTY to answer either.
log "seed headless flags in ~/.claude.json"
CLAUDE_JSON="/root/.claude.json"
[ -f "$CLAUDE_JSON" ] || echo '{}' >"$CLAUDE_JSON"
jq_edit "$CLAUDE_JSON" '
    .hasCompletedOnboarding          = true
  | .bypassPermissionsModeAccepted   = true
'

# workspace settings: trust .mcp.json project servers (os-server writes MCP
# connector entries there — internal/claudecode/mcp.go) without the interactive
# approval prompt.
log "seed workspace .claude/settings.json"
SETTINGS="$WS_DIR/.claude/settings.json"
mkdir -p "$WS_DIR/.claude"
[ -f "$SETTINGS" ] || echo '{}' >"$SETTINGS"
jq_edit "$SETTINGS" '.enableAllProjectMcpServers = true'

# ── §3 CHANNELS (computed before §2 so the .env write includes the launch flags)
# Claude Code runs telegram + discord natively via its channel plugins: the
# bridge launches `claude --channels <plugins>`, each plugin polls its Bot API
# with the token in ~/.claude/channels/<ch>/.env, and access.json gates senders.
# We seed dmPolicy=allowlist with the configured owner id — the interactive
# pairing flow (/telegram:access pair, /discord:access pair) needs a terminal
# this device does not have. Both plugins share the same access.json schema.
CHANNELS=""

# sync_channel <name> <token-env-var> <token> <owner-id> — writes the plugin's
# .env + allowlist and appends the plugin to the --channels launch list.
sync_channel() {
  local ch="$1" var="$2" token="$3" user="$4"
  local dir="$CLAUDE_HOME/channels/$ch"
  if [ -z "$token" ]; then
    log "$ch: no bot token in config.json — channel left disabled"
    return 0
  fi
  mkdir -p "$dir"
  umask 077
  printf '%s=%s\n' "$var" "$token" >"$dir/.env"
  umask 022
  local access="$dir/access.json"
  [ -f "$access" ] || echo '{}' >"$access"
  if [ -n "$user" ]; then
    jq_edit "$access" --arg id "$user" '
        .dmPolicy  = "allowlist"
      | .allowFrom = (((.allowFrom // []) + [$id]) | unique)
    '
    log "$ch enabled — token synced, allowlist seeded ($user)"
  else
    # No owner id → leave the plugin's default pairing policy; inbound stays
    # gated (headless pairing is not possible) until the user id is configured.
    log "$ch token set but owner user id missing — allowlist NOT seeded (inbound gated on pairing)"
  fi
  CHANNELS="${CHANNELS:+$CHANNELS }plugin:${ch}@claude-plugins-official"
}

sync_channel telegram TELEGRAM_BOT_TOKEN "$(dev telegram_bot_token)" "$(dev telegram_user_id)"
sync_channel discord  DISCORD_BOT_TOKEN  "$(dev discord_bot_token)"  "$(dev discord_user_id)"

# ── §2 ENV (config.json wins) ───────────────────────────────────────────────────
# Two auth modes, decided by the claude login flow (internal/claudecode/login.go):
#
#   subscription — claude_code_oauth_token set in config.json (or the CLI saved
#     ~/.claude/.credentials.json): inject CLAUDE_CODE_OAUTH_TOKEN and OMIT every
#     ANTHROPIC_* var — API-key vars OUTRANK OAuth in Claude Code's credential
#     precedence, so leaving them set would silently override the login.
#   api-key — default: campaign-api via llm_* from config.json.
OAUTH_TOKEN="$(dev claude_code_oauth_token)"
umask 077
if [ -n "$OAUTH_TOKEN" ] || [ -s "$CLAUDE_HOME/.credentials.json" ]; then
  log "write $ENV_FILE (auth=claude.ai subscription, token=$( [ -n "$OAUTH_TOKEN" ] && echo config || echo credentials.json ))"
  {
    echo "# Managed by runtime-claudecode-presync — do not edit (synced from /root/config/config.json)."
    echo "# Subscription auth: ANTHROPIC_* omitted on purpose (they outrank the OAuth login)."
    if [ -n "$OAUTH_TOKEN" ]; then
      echo "CLAUDE_CODE_OAUTH_TOKEN=$OAUTH_TOKEN"
    fi
    echo "DISABLE_AUTOUPDATER=1"
    echo "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"
    echo "CLAUDECODE_CHANNELS=$CHANNELS"
  } >"$ENV_FILE.tmp"
else
  LLM_BASE_URL="$(dev llm_base_url)"; [ -n "$LLM_BASE_URL" ] || LLM_BASE_URL="$DEFAULT_BASE_URL"
  LLM_API_KEY="$(dev llm_api_key)"
  LLM_MODEL="$(dev llm_model)"; [ -n "$LLM_MODEL" ] || LLM_MODEL="$DEFAULT_MODEL"
  log "write $ENV_FILE (auth=api-key, base_url=$LLM_BASE_URL model=$LLM_MODEL key=$( [ -n "$LLM_API_KEY" ] && echo set || echo EMPTY ))"
  cat >"$ENV_FILE.tmp" <<ENV
# Managed by runtime-claudecode-presync — do not edit (synced from /root/config/config.json).
ANTHROPIC_BASE_URL=$LLM_BASE_URL
# Both auth vars carry llm_api_key: claude sends x-api-key from ANTHROPIC_API_KEY
# and Authorization: Bearer from ANTHROPIC_AUTH_TOKEN — campaign-api accepts the
# bearer form; setting both keeps either proxy convention working.
ANTHROPIC_API_KEY=$LLM_API_KEY
ANTHROPIC_AUTH_TOKEN=$LLM_API_KEY
ANTHROPIC_MODEL=$LLM_MODEL
ANTHROPIC_SMALL_FAST_MODEL=$LLM_MODEL
DISABLE_AUTOUPDATER=1
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
CLAUDECODE_CHANNELS=$CHANNELS
ENV
fi
mv "$ENV_FILE.tmp" "$ENV_FILE"
umask 022

log "done — claudecode bridge + env + channel config synced"
