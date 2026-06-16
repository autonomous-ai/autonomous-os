# Load Balancer & Encryption (`lbserver`)

`lbserver` (`:7999`) sits between nginx and one or more `dlserver` instances. It
does two jobs:

1. **Round-robin proxy** — spreads HTTP and WebSocket traffic across the backends
   listed in `LB__BACKENDS`.
2. **Encryption terminator** — decrypts inbound RSA+AES traffic and re-encrypts
   responses, so `dlserver` only ever sees plaintext and runs no crypto itself.

```
HAL (client)                  lbserver :7999                 dlserver :8001
┌────────────┐  encrypted   ┌──────────────────┐  plaintext  ┌──────────┐
│CryptoSession│ ───────────▶│ RSAAESCrypto     │ ───────────▶│ FastAPI  │
│ AES-256-GCM │ ◀───────────│ decrypt→forward  │ ◀───────────│ (no crypto)│
└────────────┘              │ encrypt←response │             └──────────┘
                            │ round-robin      │
                            └──────────────────┘
```

## Round-robin proxy

- Backends from `LB__BACKENDS` (comma-separated, e.g.
  `http://127.0.0.1:8001,http://127.0.0.1:8002`). Empty by default → requests get
  `503 No backends configured`.
- Separate round-robin cursors for HTTP and WS, mutex-guarded.
- HTTP: all methods proxied; backend unreachable → `502`.
- WS: full-duplex proxy; backend unreachable → close `1011`.
- Timeouts: `LB__HTTP_TIMEOUT` and `LB__WS_OPEN_TIMEOUT` (both default `120.0`).

## Crypto scheme

| Component | Algorithm | Detail |
|-----------|-----------|--------|
| Key exchange | RSA-OAEP (SHA-256) | Client encrypts a random 32-byte AES session key with the LB's RSA public key |
| Data | AES-256-GCM | 12-byte nonce, 16-byte auth tag |
| Key pair | RSA 2048-bit (`CRYPTO__KEY_SIZE`) | Generated at startup; persisted to `CRYPTO__KEY_DIR` (`private_key.pem` 0o600 + `public_key.pem`) and reloaded on restart |

Constants (`src/core/crypto/constants.py`): `AES_KEY_SIZE=32`, `GCM_NONCE_SIZE=12`,
`GCM_TAG_SIZE=16`.

### Public key

```
GET /api/crypto/public-key   →  PEM (text/plain)   |  404 if crypto disabled
```

Clients fetch this once to wrap their AES session key.

### HTTP encryption

Client → LB (`CipherHTTPRequest`):

```json
{"encrypted_key": "<b64 RSA-OAEP(AES key)>", "nonce": "<b64 12B>", "cipher_data": "<b64 AES-GCM(payload+tag)>"}
```

LB → client (`CipherHTTPResponse`):

```json
{"nonce": "<b64 12B>", "cipher_data": "<b64 AES-GCM(payload+tag)>"}
```

If a POST/PUT/PATCH body doesn't parse as `CipherHTTPRequest`:
`CRYPTO__REQUIRE_ENCRYPTION=true` → `400 Encryption required`; otherwise the body is
forwarded as plaintext. A bad auth tag → `400 Decryption failed`.

### WebSocket encryption

1. First client message — key exchange (`WSKeyExchangeRequest`):

   ```json
   {"type": "key_exchange", "encrypted_key": "<b64 RSA-OAEP(AES key)>"}
   ```

   LB replies `{"status": "key_exchange_ok"}`.

2. All subsequent messages both directions (`WSCipherMessage`):

   ```json
   {"type": "encrypted", "nonce": "<b64 12B>", "cipher_data": "<b64>"}
   ```

Enforcement: the LB waits ~5 s for the key-exchange frame. With
`CRYPTO__REQUIRE_ENCRYPTION=true`, a missing/late exchange closes the socket with
`1008 Key exchange required`; a decryption failure closes with `1011`. With
`require_encryption=false`, a connection that skips the exchange passes through as
plaintext.

## Configuration

| Env var | Default | Meaning |
|---------|---------|---------|
| `CRYPTO__ENABLED` | `true` | Master switch for LB encryption |
| `CRYPTO__KEY_DIR` | `~/.dlbackend/keys` | RSA key persistence dir |
| `CRYPTO__KEY_SIZE` | `2048` | RSA key size (bits) |
| `CRYPTO__REQUIRE_ENCRYPTION` | `false` | Reject plaintext requests/connections |
| `LB__BACKENDS` | `""` | Comma-separated dlserver URLs |
| `LB__PORT` | `7999` | lbserver bind port |
| `LB__HOST` | `0.0.0.0` | lbserver bind host |
| `LB__HTTP_TIMEOUT` | `120.0` | Upstream HTTP timeout (s) |
| `LB__WS_OPEN_TIMEOUT` | `120.0` | Upstream WS handshake timeout (s) |

## nginx front door

`nginx.conf` (HTTP) listens on `:8899`; `nginx-ssl.conf` is the same with
`listen 8899 ssl` and certs at `/workspace/ssl/{cert,key}.pem`.

| Location | Proxies to | Notes |
|----------|-----------|-------|
| `/` | `127.0.0.1:7999/` | lbserver, no rewrite |
| `/lelamp/` | `127.0.0.1:7999/hal/` | device traffic; strips `/lelamp/`, prepends `/hal/` |
| `/jupyter/` | `127.0.0.1:8890/jupyter/` | dev Jupyter, unrelated |

WebSocket upgrade headers are forwarded via the `$connection_upgrade` map. Body
limit 50 MB, read/send timeouts 120 s.

> `/lelamp/` is a legacy brand prefix. Code is the source of truth — update
> `nginx.conf`/`nginx-ssl.conf` and these docs together if it is de-branded.

## Client side (HAL)

HAL's `service/sensing/crypto.py` holds the matching `CryptoSession`: it fetches the
public key, wraps an AES session key, and speaks the same `CipherHTTPRequest` /
`WSKeyExchangeRequest` / `WSCipherMessage` wire format. Relevant HAL env knobs are
listed in [configuration.md](configuration.md#hal-client-os-hal).
</content>
