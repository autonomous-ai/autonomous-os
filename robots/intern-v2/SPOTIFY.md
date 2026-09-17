# Spotify Connect on Intern V2 — pause notes

Work is paused mid-way. This file captures where we stopped so a later
session (or a teammate) can resume without re-deriving.

## What works today

- `/api/device/connectors/spotify/exchange-code` (os-server) accepts a
  browser-scraped OAuth `code` + `client_id` + `client_secret`, does the
  token exchange server-side, and persists the refresh_token via the same
  `connectorWriter` the PAT path uses. See
  `system/server/device/delivery/mqtt/connector_spotify_handler.go`.
- Device Settings page `settings:spotify` (device web UI) walks the user
  through Spotify Developer app creation → OAuth authorize → paste code →
  "Send to device" button (which hits the endpoint above). See
  `system/web/src/pages/settings/SpotifySection.tsx`.
- ecm-website `connectorAuthRegistry.tsx` has the equivalent PAT-modal
  entry `SPOTIFY_PAT`, with the same reactive guide (button + curl block
  regenerate from typed Client ID / Secret). The `PatConfig.Guide` type
  was widened to `React.FC<{ values }>` so the modal passes live field
  values in.
- `/usr/local/bin/spotify-play` (in `rootfs/`) is a self-contained CLI
  that mints an access_token from the stored refresh_token, rotates it if
  Spotify hands back a new one, picks a Connect device (defaulting to
  a name match on `intern`), searches, and starts playback. This is the
  ONLY path the `music` skill should use — hand-rolling curl to
  `api.spotify.com` fails because the on-disk field is a refresh token,
  not a Bearer.
- Skill `skills/music/SKILL.md` routes to `spotify-play` when the
  connector file has a `streaming` scope, else falls back to YouTube.
- Skill `skills/connectors/SKILL.md` has a `Spotify (special case)`
  section with the mint pattern for agents that DON'T route through the
  music skill.

## Where music actually comes out today

On the device (172.168.20.183) raspotify is installed and running as a
systemd service, authenticated via OAuth (cache at
`/var/cache/raspotify/credentials.json`). Config lives in
`rootfs/etc/systemd/system/raspotify.service.d/override.conf` — needs
`PrivateTmp=no` to reach HAL's pulseaudio socket, and
`LIBRESPOT_DEVICE=alsa_output.platform-soc_3000000_i2s3_mach.stereo-fallback`
to bypass the aec_sink (which was inaudible for music). It appears in the
Spotify Cloud device list as **"Intern 2"**.

## Not done (pick up here)

- **Audibility not fully verified**. `pactl` shows sink RUNNING / mute
  off / volume 100 %, `spotify-play --status` reports `playing=true`,
  librespot is pushed into sink 0 (raw alsa_output), but the user did
  not hear audio out of the intern's speaker. Next step: verify HAL TTS
  audio is audible via the same sink (proves speaker chain), then work
  out whether librespot's softvol is silently attenuating, or the sink 0
  master volume needs `amixer` init on this codec.
- **Audio ducking**: no coordination between HAL TTS and raspotify.
  When Intern speaks, music will bleed on top of TTS. Handler should
  either `spotify-play --pause` before speak / `--resume` after, or use
  a PulseAudio module (module-role-cork) to auto-cork music sinks while
  voice sinks are active.
- **Install path**: raspotify was installed manually with
  `curl -sSL https://dtcooper.github.io/raspotify/install.sh | bash`.
  For fleet rollout, bake it into the OrangePi image (`scripts/imager/`)
  and pre-provision `/var/cache/raspotify/credentials.json` per device
  (or ship a first-boot OAuth prompt).
- **mDNS blocked by WiFi**: the local `spotify-play` shortcut works
  regardless because raspotify is authenticated in the Cloud device
  list, but the Spotify app's `Connect` menu on the same LAN still
  cannot see "Intern 2" via zeroconf. Not blocking for the CLI flow.
- **spotify-play device filter output** used to print "on intern"
  (filter string) instead of the actual chosen device name. Already
  fixed — check `rootfs/usr/local/bin/spotify-play`.

## Manual install one-liner on a fresh Intern

```bash
# 1. raspotify
curl -sSL https://dtcooper.github.io/raspotify/install.sh | sudo bash

# 2. drop overrides + CLI (assumes this repo is on the device)
sudo cp robots/intern-v2/rootfs/usr/local/bin/spotify-play /usr/local/bin/
sudo chmod +x /usr/local/bin/spotify-play
sudo mkdir -p /etc/systemd/system/raspotify.service.d
sudo cp robots/intern-v2/rootfs/etc/systemd/system/raspotify.service.d/override.conf \
   /etc/systemd/system/raspotify.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl restart raspotify

# 3. OAuth once — needs SSH tunnel because Spotify only accepts loopback
#    redirect URIs and the browser is on the operator's laptop:
ssh -L 8899:127.0.0.1:8899 orangepi@<device-ip>
# then on device:
sudo systemctl stop raspotify
sudo librespot --name "Intern 2" --backend pulseaudio \
  --system-cache /var/cache/raspotify \
  --enable-oauth --oauth-port 8899
# copy the printed URL, open in laptop browser, approve — librespot
# writes credentials.json to /var/cache/raspotify. Ctrl-C when done.
sudo systemctl start raspotify
```
