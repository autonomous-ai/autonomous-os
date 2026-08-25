#!/usr/bin/env bash
# Shared signed OTA metadata helpers. Source from release scripts; do not run
# this file directly. The signing private key must remain outside the repo.

ota_base64_decode() {
  base64 --decode 2>/dev/null || base64 -D
}

# ota_metadata_unpack <envelope> <payload>
ota_metadata_unpack() {
  local envelope="$1" payload="$2"
  if jq -er '(.signed // .) | .format == "autonomous-ota/v1" and .signature.algorithm == "ed25519" and (.payload | type == "string")' "$envelope" >/dev/null; then
    jq -r '(.signed // .).payload' "$envelope" | ota_base64_decode >"$payload" \
      || { echo "ERROR: decode signed OTA metadata payload" >&2; return 1; }
  else
    cp "$envelope" "$payload"
  fi
  jq -e . "$payload" >/dev/null || { echo "ERROR: OTA metadata payload is not JSON" >&2; return 1; }
}

# ota_metadata_sign <payload> <envelope>
# OTA_SIGNING_PRIVATE_KEY is a PEM Ed25519 key path; OTA_SIGNING_KEY_ID labels
# the key for operators and is not used as a trust decision on devices.
ota_metadata_sign() {
  local payload="$1" envelope="$2" signature payload_b64 signature_b64
  if [ -z "${OTA_SIGNING_PRIVATE_KEY:-}" ]; then
    echo "WARN: OTA signing key is not configured; publishing legacy unsigned metadata" >&2
    cp "$payload" "$envelope"
    return 0
  fi
  : "${OTA_SIGNING_KEY_ID:?OTA_SIGNING_KEY_ID is required when OTA_SIGNING_PRIVATE_KEY is set}"
  [ -r "$OTA_SIGNING_PRIVATE_KEY" ] || { echo "ERROR: cannot read OTA_SIGNING_PRIVATE_KEY" >&2; return 1; }

  signature=$(mktemp)
  trap 'rm -f "$signature"' RETURN
  openssl pkeyutl -sign -rawin -inkey "$OTA_SIGNING_PRIVATE_KEY" -in "$payload" -out "$signature" \
    || { echo "ERROR: sign OTA metadata" >&2; return 1; }
  payload_b64=$(base64 <"$payload" | tr -d '\n')
  signature_b64=$(base64 <"$signature" | tr -d '\n')
  # Keep the payload's component entries at the top level for already deployed
  # workers. New workers use only .signed after verifying it. Set
  # OTA_METADATA_SIGNED_ONLY=1 to drop that compatibility copy once the fleet
  # has migrated: the published document then carries nothing a legacy worker
  # can consume, so an unmigrated device stops updating instead of updating
  # from an unauthenticated source.
  local base='.'
  if [ "${OTA_METADATA_SIGNED_ONLY:-0}" = "1" ]; then
    base='{}'
  fi
  jq --arg payload "$payload_b64" --arg signature "$signature_b64" --arg keyID "$OTA_SIGNING_KEY_ID" \
    "$base"' + {signed:{format:"autonomous-ota/v1", payload:$payload, signature:{algorithm:"ed25519", key_id:$keyID, value:$signature}}}' \
    "$payload" >"$envelope"
}

# ota_artifact_sha256 <file>
ota_artifact_sha256() {
  shasum -a 256 "$1" | awk '{print $1}'
}
