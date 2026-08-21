#!/usr/bin/env bash
# spike-lib.sh — shared plumbing for the Reachy Mini spike scripts.
#
# Sourced, never executed. Everything the spike scripts have in common lives
# here exactly once: fetching OTA metadata, installing a component from it, and
# writing a systemd unit.
#
# Why a library at all: the first version of spike.sh duplicated what the
# per-component scripts did, drifted from them within a week, and ended up
# deploying os-server against the wrong config directory while every service
# still reported healthy. Nothing below is copied into a caller — a caller that
# needs different behaviour changes it here, for all of them.
#
# Everything is pulled from the OTA metadata, the same source the imager and
# scripts/provision/setup.sh read. Nothing is built on a developer machine and
# copied over: that would install whatever happened to be in someone's working
# tree, which is not what the fleet runs and not what a bug report is about.

# --- Layout ------------------------------------------------------------------
# The production layout, not a spike-only one. An earlier version installed
# under /opt/autonomous and had to patch DEVICES_DIR in the .env to match; the
# shipped .env already says /opt/devices, so using the real paths removes a
# whole class of "healthy but empty capability list" failures.
DEVICE_TYPE="${DEVICE_TYPE:-reachy-mini}"
HAL_DIR="${HAL_DIR:-/opt/hal}"
DEVICES_DIR="${DEVICES_DIR:-/opt/devices}"
CONFIG_DIR="${CONFIG_DIR:-/root/config}"
# Must match openclaw's own default home (~/.openclaw for root) and the
# Default() value in system/server/config/config.go. os-server joins this with
# "openclaw.json" to find the gateway token.
OPENCLAW_CONFIG_DIR="${OPENCLAW_CONFIG_DIR:-/root/.openclaw}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
WEB_ROOT="${WEB_ROOT:-/usr/share/nginx/html/setup}"

# The Pollen daemon: owns motion, and owns the camera and ALSA PCMs until HAL
# asks for them (ROBOT.md `owner: pollen_daemon`).
DAEMON_URL="${DAEMON_URL:-http://localhost:8000}"

# Default OTA feed. Overridden by an existing bootstrap.json, or by exporting
# OTA_METADATA_URL before running any spike script.
DEFAULT_METADATA_URL="https://cdn.autonomous.ai/os/ota/metadata.json"

# All progress output goes to STDERR, never stdout. ota_field and friends are
# called inside $( ), so anything they print on stdout is captured as part of
# the value — which silently turned a version string into an apt transcript.
say() { printf '\n========== %s ==========\n' "$1" >&2; }
info() { printf '[%s] %s\n' "${SPIKE_TAG:-spike}" "$1" >&2; }
die() { printf '[%s] ERROR: %s\n' "${SPIKE_TAG:-spike}" "$1" >&2; exit 1; }

ensure_root() {
  [ "$(id -u)" -eq 0 ] || die "run as root:  sudo bash $0 $*"
}

retry() {
  local cmd="$1" max="${2:-5}" delay="${3:-2}" n=0
  until [ "$n" -ge "$max" ]; do
    eval "$cmd" && return 0
    n=$((n + 1))
    info "retry $n/$max"
    sleep "$delay"
  done
  return 1
}

# --- Prerequisites -----------------------------------------------------------

ensure_tools() {
  local missing=()
  for t in curl unzip jq; do command -v "$t" >/dev/null || missing+=("$t"); done
  [ ${#missing[@]} -eq 0 ] && return 0
  info "installing: ${missing[*]}"
  # Output to stderr for the same reason as info(): this can run underneath a
  # command substitution, and an apt transcript on stdout becomes the "value".
  apt-get update -qq >&2
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}" >&2 \
    || die "could not install ${missing[*]}"
}

# A key is trusted only when it was pinned in bootstrap.json by provisioning.
# Metadata must never be allowed to nominate its own verification key.
signing_public_key() {
  [ -f "$CONFIG_DIR/bootstrap.json" ] || return 0
  jq -r '.signing_public_key // empty' "$CONFIG_DIR/bootstrap.json" 2>/dev/null || true
}

ota_base64_decode() {
  base64 --decode 2>/dev/null || base64 -D
}

ensure_ota_verifier() {
  command -v openssl >/dev/null && return 0
  info "installing: openssl"
  apt-get update -qq >&2
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssl >&2 \
    || die "could not install openssl for OTA signature verification"
}

# verify_ota_metadata <envelope> <payload> <base64-ed25519-public-key>
# The signature covers the decoded payload bytes.  Hybrid feeds keep legacy
# entries at the top level, while verified devices consume only .signed.
verify_ota_metadata() {
  local envelope="$1" payload="$2" public_key="$3" public_der signature
  ensure_ota_verifier
  public_der="$(mktemp)"
  signature="$(mktemp)"
  trap 'rm -f "$public_der" "$signature"' RETURN
  jq -er '(.signed // .) | .format == "autonomous-ota/v1" and .signature.algorithm == "ed25519" and (.payload | type == "string")' "$envelope" >/dev/null \
    || { info "ERROR: OTA metadata is unsigned or uses an unsupported format"; return 1; }
  # SubjectPublicKeyInfo DER prefix for a raw 32-byte Ed25519 public key.
  printf '\060\052\060\005\006\003\053\145\160\003\041\000' >"$public_der"
  printf '%s' "$public_key" | ota_base64_decode >>"$public_der" \
    || { info "ERROR: bootstrap signing_public_key is not base64"; return 1; }
  [ "$(wc -c <"$public_der" | tr -d ' ')" = "44" ] \
    || { info "ERROR: bootstrap signing_public_key is not a 32-byte Ed25519 key"; return 1; }
  jq -r '(.signed // .).payload' "$envelope" | ota_base64_decode >"$payload" \
    || { info "ERROR: OTA metadata payload is not valid base64"; return 1; }
  jq -r '(.signed // .).signature.value' "$envelope" | ota_base64_decode >"$signature" \
    || { info "ERROR: OTA metadata signature is not valid base64"; return 1; }
  openssl pkeyutl -verify -pubin -keyform DER -inkey "$public_der" -rawin -in "$payload" -sigfile "$signature" >/dev/null \
    || { info "ERROR: OTA metadata signature verification failed"; return 1; }
  jq -e . "$payload" >/dev/null 2>&1 \
    || { info "ERROR: verified OTA metadata payload is not JSON"; return 1; }
}

# --- OTA metadata ------------------------------------------------------------
# The URL comes from bootstrap.json when it exists, so a robot pointed at a
# staging feed keeps using it instead of silently falling back to production.
#
# Cached in a FIXED path, not a shell variable: ota_field is called inside $( ),
# which runs in a subshell, so a variable cache is discarded every time and the
# feed gets re-fetched once per field read. A file survives that, and it also
# means the six scripts spike.sh runs all install from ONE snapshot of the feed
# — a publish landing mid-run cannot give os-server and hal mismatched builds.
# spike.sh clears it at the start of a run so each run reads fresh.
META_CACHE="${SPIKE_META_CACHE:-/tmp/.spike-ota-metadata.json}"

metadata_url() {
  if [ -n "${OTA_METADATA_URL:-}" ]; then
    echo "$OTA_METADATA_URL"
    return
  fi
  local from_bootstrap=""
  if [ -f "$CONFIG_DIR/bootstrap.json" ]; then
    from_bootstrap="$(jq -r '.metadata_url // empty' "$CONFIG_DIR/bootstrap.json" 2>/dev/null || true)"
  fi
  echo "${from_bootstrap:-$DEFAULT_METADATA_URL}"
}

fetch_metadata() {
  [ -s "$META_CACHE" ] && return 0
  ensure_tools
  local url tmp
  url="$(metadata_url)"
  tmp="$(mktemp)"
  # no-cache: the CDN serves these with long-lived edge caching, and a stale
  # metadata read installs yesterday's build while reporting today's version.
  retry "curl -fsSL -H 'Cache-Control: no-cache' -H 'Pragma: no-cache' -o '$tmp' '$url'" 5 \
    || { rm -f "$tmp"; die "could not fetch OTA metadata from $url"; }
  # Validate before caching: a captive portal or an error page returns 200 with
  # HTML, and caching that makes every later jq read come back empty with no
  # hint as to why.
  jq -e . "$tmp" >/dev/null 2>&1 || { rm -f "$tmp"; die "OTA metadata at $url is not valid JSON"; }
  local public_key payload
  public_key="$(signing_public_key)"
  if [ -n "$public_key" ]; then
    payload="$(mktemp)"
    verify_ota_metadata "$tmp" "$payload" "$public_key" || { rm -f "$tmp" "$payload"; die "could not verify OTA metadata from $url"; }
    rm -f "$tmp"
    mv "$payload" "$META_CACHE"
    info "OTA metadata signature: verified"
  else
    mv "$tmp" "$META_CACHE"
    info "WARN: OTA signing is not configured; using legacy unsigned metadata"
  fi
  info "OTA metadata: $url"
}

# Drop the snapshot so the next read re-fetches. spike.sh calls this once up
# front; a single component script does not, so re-running one of them right
# after a publish still needs the cache cleared by hand (or a new shell).
clear_metadata_cache() { rm -f "$META_CACHE"; }

# Seed $CONFIG_DIR/bootstrap.json if it is missing or carries no metadata_url.
#
# Called by BOTH spike-os.sh and spike-bootstrap.sh, and that is the point: the
# file is not only the OTA worker's config, it is where os-server reads
# OTAMetadataURL from (system/server/config/config.go — config.json deliberately
# does not persist it), and the agent runtimes' skill watchers take the URL from
# there to fetch skills (runtimes/*/skill_watcher.go → skills.FetchSkillVersions).
# With no metadata_url that fetch returns early and the device comes up with
# empty skill folders — nothing errors, the agent simply has no abilities.
#
# So it cannot wait for the bootstrap step, which runs last on purpose. Seeding
# is idempotent, so the step that owns the worker calls it too.
ensure_bootstrap_config() {
  ensure_tools
  local url key bs tmp
  url="$(metadata_url)"
  key="${OTA_SIGNING_PUBLIC_KEY:-}"
  bs="$CONFIG_DIR/bootstrap.json"
  mkdir -p "$CONFIG_DIR" /root/bootstrap

  if [ -f "$bs" ]; then
    # Merge-if-empty, never clobber: an operator who pointed this robot at a
    # staging feed must not be moved back to production by a re-run.
    tmp="$(mktemp)"
    if jq --arg url "$url" --arg key "$key" \
        'if (.metadata_url // "") == "" then .metadata_url = $url else . end | if (.signing_public_key // "") == "" and $key != "" then .signing_public_key = $key else . end' \
        "$bs" >"$tmp" 2>/dev/null; then
      mv "$tmp" "$bs"
      info "bootstrap.json: metadata_url=$(jq -r '.metadata_url // "(none)"' "$bs")"
    else
      rm -f "$tmp"
      info "WARN: $bs is not valid JSON — leaving it untouched"
    fi
    return 0
  fi

  jq -n --arg url "$url" --arg key "$key" \
    '{httpPort: 8080, metadata_url: $url, signing_public_key: $key, poll_interval: "5m", state_file: "/root/bootstrap/state.json"}' \
    >"$bs"
  info "seeded $bs with metadata_url=$url"
}

# ota_field <component> <field>
# Components are top-level keys ("hal", "os-server", "web", "bootstrap",
# "openclaw"); the device profile is nested under devices.<type>.
ota_field() {
  local component="$1" field="$2"
  fetch_metadata
  if [ "$component" = "device" ]; then
    jq -r --arg t "$DEVICE_TYPE" --arg f "$field" '.devices[$t][$f] // empty' "$META_CACHE"
  else
    jq -r --arg c "$component" --arg f "$field" '.[$c][$f] // empty' "$META_CACHE"
  fi
}

# ota_unpack <component> <dest_dir>
# Downloads the component zip and unpacks it INTO dest_dir. Prints the version.
ota_unpack() {
  local component="$1" dest="$2"
  local url version sha256 zip_tmp public_key
  url="$(ota_field "$component" url)"
  version="$(ota_field "$component" version)"
  sha256="$(ota_field "$component" sha256)"
  [ -n "$url" ] || die "OTA metadata has no url for '$component' (device_type=$DEVICE_TYPE)"
  public_key="$(signing_public_key)"
  if [ -n "$public_key" ] && ! [[ "$sha256" =~ ^[a-fA-F0-9]{64}$ ]]; then
    die "OTA metadata has no valid SHA-256 for '$component'"
  fi

  zip_tmp="$(mktemp)"
  retry "curl -fsSL -H 'Cache-Control: no-cache' -H 'Pragma: no-cache' -o '$zip_tmp' '$url'" 5 \
    || die "download failed: $component from $url"
  if [ -n "$public_key" ]; then
    echo "$sha256  $zip_tmp" | sha256sum -c - >/dev/null \
      || { rm -f "$zip_tmp"; die "SHA-256 mismatch: $component"; }
  fi
  mkdir -p "$dest"
  unzip -o -q "$zip_tmp" -d "$dest" || die "unzip failed: $component"
  rm -f "$zip_tmp"
  info "$component $version -> $dest"
}

# ota_install_binary <component> <dest_path>
# For components shipped as a zipped single binary (os-server, bootstrap).
ota_install_binary() {
  local component="$1" dest="$2"
  local dir_tmp bin
  dir_tmp="$(mktemp -d)"
  ota_unpack "$component" "$dir_tmp"
  # The zip has held the binary at the root and one level down at different
  # times; prefer an executable, fall back to the only file present.
  bin="$(find "$dir_tmp" -type f -perm -u+x 2>/dev/null | head -1)"
  [ -n "$bin" ] || bin="$(find "$dir_tmp" -type f 2>/dev/null | head -1)"
  [ -n "$bin" ] || die "no binary inside the $component zip"
  # Replacing a running binary in place gives ETXTBSY — the caller stops the
  # unit first, but a stray process can still hold it, so install by rename.
  install -m 0755 "$bin" "$dest.new" && mv -f "$dest.new" "$dest"
  rm -rf "$dir_tmp"
}

# --- systemd -----------------------------------------------------------------

# write_unit <name> <<'UNIT' ... UNIT
# Reads the unit body from stdin so callers keep it as a readable heredoc.
write_unit() {
  local name="$1"
  cat >"/etc/systemd/system/${name}.service"
  systemctl daemon-reload
  info "wrote /etc/systemd/system/${name}.service"
}

start_unit() {
  local name="$1"
  systemctl enable "$name" >/dev/null 2>&1
  systemctl restart "$name"
}

stop_unit() {
  local name="$1"
  systemctl stop "$name" 2>/dev/null || true
}

remove_unit() {
  local name="$1"
  systemctl disable --now "$name" 2>/dev/null || true
  rm -f "/etc/systemd/system/${name}.service"
  systemctl daemon-reload
}

# wait_http <url> <seconds> [label]
wait_http() {
  local url="$1" secs="${2:-60}" label="${3:-service}" i=0
  printf '[%s] waiting for %s ' "${SPIKE_TAG:-spike}" "$label"
  while [ "$i" -lt "$secs" ]; do
    if curl -sf -m 3 "$url" >/dev/null 2>&1; then echo " up"; return 0; fi
    printf '.'
    sleep 2
    i=$((i + 2))
  done
  echo " TIMEOUT"
  return 1
}
