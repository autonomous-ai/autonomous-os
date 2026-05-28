"""
End-to-end test of `SpeechEmotionService` on a local machine.

What it exercises:
  - Mic capture (sounddevice) → in-process `submit()`
  - Worker thread → POST dlbackend `/api/dl/ser/recognize`
  - Per-user buffer + polarity-bucket dedup
  - Flush thread → POST sensing event to Lamp

To avoid needing a running Lamp instance on the dev machine, the script
spins up a tiny mock HTTP listener on `127.0.0.1:5000` that captures
every `/api/sensing/event` POST and prints it. Override with --lamp-url
to talk to a real Lamp instead.

Usage (from repo root):

    export DL_BACKEND_URL="https://<host>"
    export DL_API_KEY="<your key>"

    # Default: record 3 clips of 3s each, all attributed to "alice"
    python -m lelamp.test.test_speech_emotion_service

    # Faster flush so the run finishes quickly
    LELAMP_SPEECH_EMOTION_FLUSH_S=3 LELAMP_SPEECH_EMOTION_MIN_AUDIO_S=2 \\
        python -m lelamp.test.test_speech_emotion_service --reps 3 --duration 3

    # Submit as 'unknown' to verify the unknown-collapse path
    python -m lelamp.test.test_speech_emotion_service --user unknown --reps 2

    # Point at a real Lamp
    python -m lelamp.test.test_speech_emotion_service \\
        --lamp-url http://192.168.1.42:5000/api/sensing/event

Run the engine-only script first
(`python -m lelamp.test.test_speech_emotion_engine`) to confirm
connectivity to dlbackend before attempting this one.
"""

from __future__ import annotations

import argparse
import http.server
import io
import json
import logging
import os
import sys
import threading
import time
import wave
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test.speech_emotion_service")

SAMPLE_RATE = 16000
CHANNELS = 1
MOCK_LAMP_HOST = "127.0.0.1"
MOCK_LAMP_PORT = 5000
# Hit FastAPI directly. Production prefix `/lelamp/api/dl/ser/recognize`
# only works when nginx fronts dlbackend (RunPod) and strips `/lelamp/`.
# Local dev hits uvicorn straight on its port, no prefix.
DEFAULT_SER_ENDPOINT = "/api/dl/ser/recognize"


# --- Mock Lamp listener ---------------------------------------------------

class _CapturedPost:
    def __init__(self, path: str, payload: dict):
        self.path = path
        self.payload = payload

    def __repr__(self) -> str:
        return f"<POST {self.path} payload={self.payload}>"


class _MockLampHandler(http.server.BaseHTTPRequestHandler):
    captured: list[_CapturedPost] = []

    def do_POST(self) -> None:  # noqa: N802 (http.server contract)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"_raw": body}
        _MockLampHandler.captured.append(_CapturedPost(self.path, payload))
        logger.info("[mock-lamp] received %s payload=%s", self.path, payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":1,"data":null,"message":null}')

    def log_message(self, fmt: str, *args) -> None:  # silence default access log
        return


def _start_mock_lamp() -> http.server.HTTPServer:
    server = http.server.HTTPServer(
        (MOCK_LAMP_HOST, MOCK_LAMP_PORT), _MockLampHandler,
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info(
        "Mock Lamp listening on http://%s:%d  (override with --lamp-url)",
        MOCK_LAMP_HOST, MOCK_LAMP_PORT,
    )
    return server


# --- Mic capture ----------------------------------------------------------

def record_wav_bytes(duration_s: float, device: int | None) -> bytes:
    import numpy as np
    import sounddevice as sd

    frames = int(duration_s * SAMPLE_RATE)
    logger.info(
        ">>> Recording %.1fs (device=%s) — speak NOW",
        duration_s, device if device is not None else "default",
    )
    pcm = sd.rec(
        frames, samplerate=SAMPLE_RATE, channels=CHANNELS,
        dtype=np.int16, device=device,
    )
    sd.wait()
    logger.info("<<< Recording done (%d samples)", len(pcm))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


# --- Main -----------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="E2E test of SpeechEmotionService")
    parser.add_argument(
        "--dl-backend-url",
        default=os.environ.get("DL_BACKEND_URL", "http://localhost:8008"),
        help="Base URL of the hosted dlbackend.",
    )
    parser.add_argument(
        "--api-key", default=os.environ.get("DL_API_KEY", ""),
        help="X-API-Key header (if dlbackend requires it).",
    )
    parser.add_argument(
        "--endpoint", default=os.environ.get("DL_SER_ENDPOINT", "") or DEFAULT_SER_ENDPOINT,
        help=f"SER endpoint path. Default {DEFAULT_SER_ENDPOINT} (FastAPI "
             "direct — no nginx prefix).",
    )
    parser.add_argument(
        "--lamp-url", default="",
        help="Override Lamp sensing URL. Default: spin up a local mock on "
             f"http://{MOCK_LAMP_HOST}:{MOCK_LAMP_PORT}/api/sensing/event",
    )
    parser.add_argument("--user", default="alice",
                        help="Identifier to attribute each clip to. Use "
                             "'unknown' to exercise the unknown-collapse path.")
    parser.add_argument("--reps", type=int, default=3,
                        help="How many clips to record + submit (default 3).")
    parser.add_argument("--duration", type=float, default=3.0,
                        help="Per-clip duration in seconds (default 3.0).")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="Pause between recordings (default 1s).")
    parser.add_argument("--device", type=int, default=None,
                        help="sounddevice input index (see `python -m sounddevice`).")
    args = parser.parse_args()

    if not args.dl_backend_url:
        print(
            "ERROR: --dl-backend-url is required (or set DL_BACKEND_URL).",
            file=sys.stderr,
        )
        return 2

    # Make sure the service picks up the right backend URL + key + endpoint.
    # We poke lelamp.config BEFORE importing SpeechEmotionService so the
    # module's top-level _API_URL / _API_KEY captures the overrides.
    #
    # `args.endpoint` defaults to the no-prefix FastAPI path so local dev
    # (uvicorn without nginx) works out of the box. Override with
    # --endpoint /lelamp/api/dl/ser/recognize when hitting a production
    # deployment that fronts dlbackend with nginx.
    from lelamp import config as _cfg
    _cfg.DL_BACKEND_URL = args.dl_backend_url
    _cfg.DL_API_KEY = args.api_key
    _cfg.DL_SER_ENDPOINT = args.endpoint
    _cfg.SPEECH_EMOTION_API_URL = (
        args.dl_backend_url.rstrip("/") + "/" + args.endpoint.strip("/")
    )
    _cfg.SPEECH_EMOTION_API_KEY = args.api_key
    logger.info("Resolved SER URL: %s", _cfg.SPEECH_EMOTION_API_URL)

    # Mock Lamp unless --lamp-url given.
    server = None
    if not args.lamp_url:
        server = _start_mock_lamp()
        args.lamp_url = f"http://{MOCK_LAMP_HOST}:{MOCK_LAMP_PORT}/api/sensing/event"
    _cfg.LAMP_SENSING_URL = args.lamp_url

    # Import AFTER config patch so module-level defaults see the right values.
    from lelamp.service.voice.speech_emotion import SpeechEmotionService

    svc = SpeechEmotionService()
    if not svc.available:
        print(
            "ERROR: SpeechEmotionService reports unavailable. Check "
            "DL_BACKEND_URL + that dlbackend is reachable.",
            file=sys.stderr,
        )
        return 3

    logger.info("Service state at start: %s", svc.to_dict())

    # Recording loop.
    for i in range(1, args.reps + 1):
        logger.info("===== clip %d / %d =====", i, args.reps)
        try:
            wav_bytes = record_wav_bytes(args.duration, args.device)
        except Exception:
            logger.exception("recording failed")
            continue
        svc.submit(user=args.user, wav_bytes=wav_bytes, duration_s=args.duration)
        time.sleep(args.pause)

    # Wait for at least one flush tick to fire (FLUSH_S + slack).
    flush_wait = svc._flush_s + 2.0  # type: ignore[attr-defined]
    logger.info("Waiting %.1fs for flush thread to drain buffer...", flush_wait)
    time.sleep(flush_wait)

    logger.info("Service state at end: %s", svc.to_dict())

    # Final report.
    print()
    print("=" * 60)
    print(f"Submitted clips     : {args.reps}")
    print(f"Lamp POSTs captured : {len(_MockLampHandler.captured)}")
    for cap in _MockLampHandler.captured:
        print(f"  - {cap.path}  type={cap.payload.get('type')}  "
              f"user={cap.payload.get('current_user')}")
        print(f"    message={cap.payload.get('message')!r}")
    print("=" * 60)
    print()
    print("Tips:")
    print("  - Nothing captured? Check the log for `[speech_emotion] DROP`")
    print("    lines — most likely 'duration < min' (raise --duration) or")
    print("    'all neutral' (try a clearly emotional reading).")
    print("  - dedup drops? Run the service with")
    print("    LELAMP_SPEECH_EMOTION_DEDUP_WINDOW_S=5 to retest quickly.")

    svc.stop()
    if server is not None:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
