"""
Music Service — search YouTube and stream audio through the speaker.

Uses yt-dlp to search and resolve YouTube audio URLs, ffmpeg to decode
and output directly to ALSA device (bypassing sounddevice/PortAudio).
"""

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from hal.clock import device_fromtimestamp, device_now
from typing import Optional

logger = logging.getLogger("hal.voice.music")
logger.setLevel(logging.DEBUG)

# Per-user audio history — JSONL logs under /root/local/users/{person}/audio_history/
_USERS_DIR = Path(os.environ.get("HAL_USERS_DIR", "/root/local/users"))
_HISTORY_MAX_DAYS = 30


def _slugify_person(person: str) -> str:
    """Match FaceRecognizer.normalize_label so writer/reader agree on folder names."""
    s = person.strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "_", s)
    s = s.strip("_")
    return s[:64] if s else "unknown"


def canonicalize_person(person: str) -> str:
    """Resolve a raw person label (from AI, Telegram sender, etc.) to a canonical
    user folder name. Tries, in order:

      1. Exact slug match on an existing user dir.
      2. Telegram id extracted from `NAME (123456)` → scan metadata.json files.
      3. Longest alphanumeric token that matches an existing user dir
         (e.g. "i am gray" → "gray").
      4. Slug fallback (may create a new dir, but at least stays filesystem-safe).
    """
    if not person:
        return "unknown"
    slug = _slugify_person(person)
    if _USERS_DIR.is_dir():
        if slug and (_USERS_DIR / slug).is_dir():
            return slug
        m = re.search(r"\((\d+)\)", person)
        if m:
            tid = m.group(1)
            for d in _USERS_DIR.iterdir():
                if not d.is_dir():
                    continue
                meta_file = d / "metadata.json"
                if not meta_file.is_file():
                    continue
                try:
                    meta = json.loads(meta_file.read_text())
                except Exception:
                    continue
                if str(meta.get("telegram_id") or "") == tid:
                    return d.name
        tokens = re.findall(r"[a-z0-9]+", person.lower())
        tokens.sort(key=len, reverse=True)
        for tok in tokens:
            if (_USERS_DIR / tok).is_dir():
                return tok
    return slug


def _history_dir(person: str = "") -> Path:
    """Return history directory for a person, or 'unknown' fallback."""
    return _USERS_DIR / canonicalize_person(person) / "audio_history"


def _history_path(person: str = "", date_str: str | None = None) -> Path:
    """Return path to daily history JSONL file."""
    if date_str is None:
        date_str = device_now().strftime("%Y-%m-%d")
    return _history_dir(person) / f"{date_str}.jsonl"


def _log_play_event(
    query: str,
    title: str | None,
    started_at: float,
    ended_at: float,
    stopped_by: str,
    person: str = "",
) -> None:
    """Append a play event to today's history file (per-user if person is set)."""
    try:
        person = canonicalize_person(person) if person else person
        hist_dir = _history_dir(person)
        hist_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": started_at,
            "date": device_fromtimestamp(started_at).strftime("%Y-%m-%d"),
            "hour": device_fromtimestamp(started_at).hour,
            "query": query,
            "title": title or "",
            "duration_s": round(ended_at - started_at, 1),
            "stopped_by": stopped_by,  # "user" | "end" | "tts" | "error" | "next"
            "person": person,
        }
        path = _history_path(person, entry["date"])
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.debug("Audio history logged: %s (%s) person=%s", title, stopped_by, person or "shared")
    except Exception as e:
        logger.warning("Failed to log audio history: %s", e)


def _cleanup_old_history() -> None:
    """Remove history files older than _HISTORY_MAX_DAYS across all users."""
    try:
        if not _USERS_DIR.exists():
            return
        cutoff = time.time() - (_HISTORY_MAX_DAYS * 86400)
        for f in _USERS_DIR.rglob("audio_history/*.jsonl"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                logger.debug("Cleaned up old history: %s", f)
    except Exception as e:
        logger.warning("History cleanup failed: %s", e)


def query_play_history(person: str = "", date_str: str | None = None, last: int = 50) -> list[dict]:
    """Read play history for a person on a given date. Returns most recent `last` entries."""
    path = _history_path(person, date_str)
    if not path.exists():
        return []
    entries = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except Exception as e:
        logger.warning("Failed to read history %s: %s", path, e)
    return entries[-last:]


def _detect_alsa_output_device() -> str:
    """ALSA output device for music playback.

    Prefer HAL_AUDIO_OUTPUT_ALSA — the per-device speaker alias from
    /etc/asound.conf (e.g. 'plug:device_speaker') — so music plays through the
    SAME speaker as TTS. The device's asound.conf is the single source of truth
    for where the speaker actually is; auto-detection by card name can't know
    which card is wired to the amp (e.g. on Lamp the onboard codec is present but
    its line-out is disconnected — only the USB DAC reaches the speaker).

    Fall back to keyword auto-detect only when the env is unset (dev/test):
    priority CD002 > Seeed ReSpeaker > onboard codec > any USB audio device.
    Returns plughw:CARD,0 for direct hardware access (handles sample rate
    conversion), or "default" as last resort.
    """
    env_dev = os.environ.get("HAL_AUDIO_OUTPUT_ALSA")
    if env_dev:
        logger.info("ALSA output: using HAL_AUDIO_OUTPUT_ALSA=%s", env_dev)
        return env_dev
    try:
        result = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return "default"
        speaker_keywords = ["cd002", "seeed", "wm8960", "sndi2s4", "es8389", "usb audio"]
        for keyword in speaker_keywords:
            for line in result.stdout.splitlines():
                if not line.startswith("card "):
                    continue
                if keyword not in line.lower():
                    continue
                m = re.search(r"card \d+: (\S+)", line)
                if m:
                    card = m.group(1)
                    logger.info("Detected ALSA output: plughw:%s,0 (matched '%s')", card, keyword)
                    return f"plughw:{card},0"
    except Exception as e:
        logger.warning("ALSA device detection failed: %s", e)
    logger.info("ALSA output: using default")
    return "default"


# Detect at import time so it's logged during service startup.
# plughw:CARD,0 handles sample rate conversion natively; music and TTS
# use the device exclusively but the service serialises them (music pauses
# while TTS speaks, so no simultaneous access).
ALSA_DEVICE = _detect_alsa_output_device()

# YouTube extraction is flaky (intermittent throttling / bot-detection, esp. from
# datacenter/VN IPs, plus the runtime EJS-challenge fetch). A single failure isn't
# a dead video — a retry usually clears it. These bound the retries so a genuinely
# unavailable video still fails fast.
MUSIC_RESOLVE_TRIES = 2       # yt-dlp search/resolve attempts
MUSIC_STREAM_TRIES = 2        # stream-start attempts (re-resolves URL between tries)
MUSIC_RETRY_BACKOFF_S = 1.5   # wait between attempts
# A stream that dies within this window of starting is a startup failure (bad/empty
# yt-dlp output -> ffmpeg "Invalid data") worth retrying; a stream that ran longer
# and then errored is treated as a real end (network drop mid-song), not retried.
MUSIC_STREAM_PROBE_S = 1.5


class MusicService:
    """YouTube music search + streaming playback via yt-dlp + ffmpeg + ALSA."""

    def __init__(
        self,
        tts_service=None,
        alsa_device: str = ALSA_DEVICE,
        on_complete=None,
    ):
        self._tts_service = tts_service
        self._alsa_device = alsa_device
        self._on_complete = on_complete

        self._lock = threading.Lock()
        self._playing = False
        self._stop_event = threading.Event()
        self._ytdlp_proc: Optional[subprocess.Popen] = None
        self._ytdlp_stderr = None  # TemporaryFile capturing the stream yt-dlp's stderr
        self._aplay_stderr = None  # TemporaryFile capturing aplay's (ALSA out) stderr
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._aplay_proc: Optional[subprocess.Popen] = None
        self._current_title: Optional[str] = None
        # Per-play callback fired when ffmpeg actually starts (after yt-dlp resolves URL).
        # Set by play() each call; cleared after firing.
        self._on_started = None
        _cleanup_old_history()

    @property
    def available(self) -> bool:
        return True

    @property
    def playing(self) -> bool:
        if not self._playing:
            return False
        # Music thread holds _lock for its entire run, including the yt-dlp
        # resolve phase (1-5s before procs spawn) where all _*_proc fields
        # are still None. Treat lock-held as "playing" so TTS rejection stays
        # in effect during resolve. Self-heal only when no thread is in flight.
        if self._lock.locked():
            return True
        procs = [self._aplay_proc, self._ffmpeg_proc, self._ytdlp_proc]
        if all(p is None or p.poll() is not None for p in procs):
            logger.warning("playing flag stuck True with no music thread — self-healing")
            self._playing = False
            return False
        return True

    @property
    def current_title(self) -> Optional[str]:
        return self._current_title

    def play(self, query: str, on_started=None, person: str = "") -> bool:
        """Search YouTube and play first result. Returns True if started.

        on_started: optional callable fired once ffmpeg begins streaming
        (i.e. after yt-dlp resolves the URL). Use this to synchronize
        visual effects (e.g. groove animation) with actual audio start.
        person: who requested the music (for per-user history).
        """
        # Stop current playback if any
        if self._playing:
            self.stop()
            time.sleep(0.3)

        if not self._lock.acquire(blocking=False):
            logger.info("Music busy, skipping: %s", query[:80])
            return False

        self._on_started = on_started
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._play_sync,
            args=(query, person),
            daemon=True,
            name="music-play",
        )
        thread.start()
        return True

    def play_file(self, path: str, title: Optional[str] = None, on_started=None, person: str = "") -> bool:
        """Play a local audio file directly via ffmpeg. Returns True if started.

        on_started: optional callable fired once ffmpeg begins streaming.
        """
        if self._playing:
            self.stop()
            time.sleep(0.3)

        if not self._lock.acquire(blocking=False):
            logger.info("Music busy, skipping file: %s", path)
            return False

        self._on_started = on_started
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._play_file_sync,
            args=(path, title, person),
            daemon=True,
            name="music-play-file",
        )
        thread.start()
        return True

    @staticmethod
    def _terminate_proc(proc):
        # SIGTERM then SIGKILL fallback. ffmpeg installs a SIGTERM handler
        # that tries to flush stderr on shutdown — if stderr is already
        # blocked, terminate() hangs forever. SIGKILL can't be caught.
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
        except Exception:
            pass

    def stop(self):
        """Stop current playback."""
        self._stop_event.set()
        for proc in [self._aplay_proc, self._ffmpeg_proc, self._ytdlp_proc]:
            self._terminate_proc(proc)

    def _play_file_sync(self, path: str, title: Optional[str] = None, person: str = ""):
        """Play a local audio file via ffmpeg directly to ALSA."""
        try:
            self._playing = True
            self._current_title = title or path.split("/")[-1]
            _started_at = time.time()
            _stopped_by = "end"

            # Wait if TTS is speaking
            if self._tts_service and self._tts_service.speaking:
                logger.info("Waiting for TTS to finish before playing file")
                for _ in range(100):  # max 10s
                    if not self._tts_service.speaking or self._stop_event.is_set():
                        break
                    time.sleep(0.1)
                time.sleep(0.5)

            if self._stop_event.is_set():
                return

            # Release TTS persistent stream so aplay can grab the ALSA device
            # exclusively. TTS reopens lazily on the next speak() call.
            if self._tts_service and hasattr(self._tts_service, "release_stream"):
                self._tts_service.release_stream()
                time.sleep(0.1)

            logger.info("Playing local file: '%s'", path)
            self._ffmpeg_proc = subprocess.Popen(
                [
                    "ffmpeg",
                    "-hide_banner", "-loglevel", "error", "-nostats",
                    "-i", path,
                    "-ac", "2",
                    "-ar", "44100",
                    "-f", "wav",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._aplay_proc = subprocess.Popen(
                ["aplay", "-D", self._alsa_device, "-q"],
                stdin=self._ffmpeg_proc.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._ffmpeg_proc.stdout.close()

            if self._on_started:
                try:
                    self._on_started()
                except Exception as e:
                    logger.warning("on_started callback failed: %s", e)
                self._on_started = None

            while not self._stop_event.is_set():
                ret = self._ffmpeg_proc.poll()
                if ret is not None:
                    if ret != 0:
                        stderr = self._ffmpeg_proc.stderr.read().decode(errors="replace")
                        logger.error("ffmpeg exited with code %d: %s", ret, stderr[-500:])
                        _stopped_by = "error"
                    break
                if self._tts_service and self._tts_service.speaking:
                    logger.debug("Pausing file playback for TTS")
                    self._ffmpeg_proc.terminate()
                    _stopped_by = "tts"
                    break
                time.sleep(0.2)

            if self._stop_event.is_set():
                _stopped_by = "user"

            logger.info("File playback ended")

        except Exception as e:
            logger.error("File play failed: %s (type=%s)", e, type(e).__name__)
            _stopped_by = "error"
        finally:
            for proc in [self._aplay_proc, self._ffmpeg_proc]:
                self._terminate_proc(proc)
            self._aplay_proc = None
            _log_play_event(path, self._current_title, _started_at, time.time(), _stopped_by, person)
            self._ffmpeg_proc = None
            self._playing = False
            self._current_title = None
            self._lock.release()
            if self._on_complete:
                try:
                    self._on_complete()
                except Exception as e:
                    logger.warning("on_complete callback failed: %s", e)
            # User-facing feedback on failure — silent failure is worse, user
            # can't tell if the agent ignored the command or just failed.
            if _stopped_by == "error" and self._tts_service and self._tts_service.available:
                try:
                    self._tts_service.speak_cached("Sorry, I can't play that right now.")
                except Exception as e:
                    logger.warning("failure apology speak failed: %s", e)

    def _play_sync(self, query: str, person: str = ""):
        """Search, resolve audio URL, play via ffmpeg directly to ALSA."""
        _started_at = time.time()
        _stopped_by = "end"
        try:
            self._playing = True

            # Resolve audio URL via yt-dlp
            logger.info("Searching YouTube: '%s'", query[:80])
            audio_url, title = self._resolve_audio_url(query)
            if not audio_url:
                logger.error("No audio URL found for: '%s'", query[:80])
                _stopped_by = "error"
                return

            self._current_title = title

            # Wait if TTS is speaking (TTS has priority, shares ALSA device)
            if self._tts_service and self._tts_service.speaking:
                logger.info("Waiting for TTS to finish before playing music")
                for _ in range(100):  # max 10s
                    if not self._tts_service.speaking or self._stop_event.is_set():
                        break
                    time.sleep(0.1)
                # Extra wait for ALSA device to be fully released
                time.sleep(0.5)

            if self._stop_event.is_set():
                return

            # Release TTS persistent stream so aplay can grab the ALSA device
            # exclusively. TTS reopens lazily on the next speak() call.
            if self._tts_service and hasattr(self._tts_service, "release_stream"):
                self._tts_service.release_stream()
                time.sleep(0.1)

            # Stream: yt-dlp stdout -> ffmpeg stdin -> ALSA (no temp file).
            # Retry stream start on early failure (transient YouTube throttling) —
            # re-resolve the URL each retry since the stream URL can be stale.
            logger.info("Starting playback: '%s'", title[:80] if title else query[:80])
            started = False
            for stream_try in range(MUSIC_STREAM_TRIES):
                if self._stop_event.is_set():
                    return
                if stream_try > 0:
                    time.sleep(MUSIC_RETRY_BACKOFF_S)
                    logger.info("Music stream retry %d/%d: '%s'",
                                stream_try + 1, MUSIC_STREAM_TRIES, query[:60])
                    audio_url, title = self._resolve_audio_url(query)
                    if not audio_url:
                        continue
                    self._current_title = title
                if self._start_stream(audio_url):
                    started = True
                    break
                # Tear down the failed attempt's procs before retrying.
                for proc in [self._aplay_proc, self._ffmpeg_proc, self._ytdlp_proc]:
                    self._terminate_proc(proc)
                self._aplay_proc = self._ffmpeg_proc = self._ytdlp_proc = None

            if not started:
                logger.error("Music stream failed to start after %d tries: '%s'",
                             MUSIC_STREAM_TRIES, query[:60])
                _stopped_by = "error"
                return

            # Notify caller that audio is actually playing (stream confirmed healthy).
            if self._on_started:
                try:
                    self._on_started()
                except Exception as e:
                    logger.warning("on_started callback failed: %s", e)
                self._on_started = None

            # Wait for ffmpeg to finish or stop signal
            while not self._stop_event.is_set():
                ret = self._ffmpeg_proc.poll()
                if ret is not None:
                    if ret != 0:
                        stderr = self._ffmpeg_proc.stderr.read().decode(errors="replace")
                        # Pair ffmpeg's error with yt-dlp's real stderr (B): without
                        # it a stream failure only shows as "Invalid data".
                        yt_err = self._read_ytdlp_stderr()
                        logger.error("ffmpeg exited with code %d: %s | yt-dlp: %s | aplay: %s",
                                     ret, stderr[-400:], yt_err[-300:],
                                     self._read_aplay_stderr()[-300:])
                        _stopped_by = "error"
                    break
                # Pause playback while TTS is speaking
                if self._tts_service and self._tts_service.speaking:
                    logger.debug("Pausing music for TTS")
                    self._ffmpeg_proc.terminate()
                    _stopped_by = "tts"
                    break
                time.sleep(0.2)

            if self._stop_event.is_set():
                _stopped_by = "user"

            logger.info("Music playback ended")

        except Exception as e:
            logger.error("Music play failed: %s (type=%s)", e, type(e).__name__)
            _stopped_by = "error"
        finally:
            for proc in [self._aplay_proc, self._ffmpeg_proc, self._ytdlp_proc]:
                self._terminate_proc(proc)
            _log_play_event(query, self._current_title, _started_at, time.time(), _stopped_by, person)
            for _f in (self._ytdlp_stderr, self._aplay_stderr):
                if _f is not None:
                    try:
                        _f.close()
                    except Exception:
                        pass
            self._ytdlp_stderr = None
            self._aplay_stderr = None
            self._aplay_proc = None
            self._ffmpeg_proc = None
            self._ytdlp_proc = None
            self._playing = False
            self._current_title = None
            self._lock.release()
            if self._on_complete:
                try:
                    self._on_complete()
                except Exception as e:
                    logger.warning("on_complete callback failed: %s", e)
            # User-facing feedback on failure — silent failure is worse, user
            # can't tell if the agent ignored the command or just failed.
            if _stopped_by == "error" and self._tts_service and self._tts_service.available:
                try:
                    self._tts_service.speak_cached("Sorry, I can't play that right now.")
                except Exception as e:
                    logger.warning("failure apology speak failed: %s", e)

    def _read_ytdlp_stderr(self) -> str:
        """Tail of the stream yt-dlp's captured stderr, or "" if none. The real
        reason a stream produced no decodable audio (vs ffmpeg's generic 'Invalid
        data'). Captured to a temp file (not a pipe) so a chatty yt-dlp can't block
        on a full stderr buffer mid-stream."""
        f = self._ytdlp_stderr
        if f is None:
            return ""
        try:
            f.seek(0)
            return f.read().decode(errors="replace")
        except Exception:
            return ""

    def _read_aplay_stderr(self) -> str:
        """Tail of aplay's (ALSA output) captured stderr, or "" if none. Tells a
        broken-pipe cascade apart at its source: 'device busy'/'cannot open' =
        contention (software), 'underrun'/write error = driver/hardware. Captured
        to a temp file so aplay can't block on a full stderr buffer mid-stream."""
        f = self._aplay_stderr
        if f is None:
            return ""
        try:
            f.seek(0)
            return f.read().decode(errors="replace")
        except Exception:
            return ""

    def _start_stream(self, audio_url: str) -> bool:
        """Start yt-dlp -> ffmpeg -> aplay and probe for an early failure.

        Returns True if the pipeline survives the startup probe (audio is flowing),
        False if ffmpeg dies during startup — the sign of bad/empty yt-dlp output
        worth retrying. On False the real yt-dlp stderr is logged (B). Procs stay
        assigned to self._*_proc for the caller to use (success) or tear down."""
        self._ytdlp_stderr = tempfile.TemporaryFile()
        self._ytdlp_proc = subprocess.Popen(
            [
                sys.executable, "-m", "yt_dlp",
                "--js-runtimes", "node:/usr/bin/node",
                "--remote-components", "ejs:github",
                "-f", "bestaudio",
                "-o", "-",
                audio_url,
            ],
            stdout=subprocess.PIPE,
            stderr=self._ytdlp_stderr,
        )
        # ffmpeg decodes to raw PCM, aplay handles ALSA output.
        # Direct ffmpeg -f alsa causes distorted audio on some wm8960 boards.
        self._ffmpeg_proc = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error", "-nostats",
                "-threads", "1",
                "-i", "pipe:0",
                "-ac", "2",
                "-ar", "44100",
                "-f", "wav",
                "pipe:1",
            ],
            stdin=self._ytdlp_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._aplay_stderr = tempfile.TemporaryFile()
        self._aplay_proc = subprocess.Popen(
            ["aplay", "-D", self._alsa_device, "-q"],
            stdin=self._ffmpeg_proc.stdout,
            stdout=subprocess.DEVNULL,
            stderr=self._aplay_stderr,
        )
        self._ffmpeg_proc.stdout.close()
        self._ytdlp_proc.stdout.close()

        # Startup probe: if ffmpeg exits within the window it never got decodable
        # audio (bad/empty yt-dlp output) — a startup failure worth retrying. A
        # stream that survives the probe is healthy; a later exit is a real end.
        deadline = time.time() + MUSIC_STREAM_PROBE_S
        while time.time() < deadline:
            if self._stop_event.is_set():
                return True  # stop requested — caller's finally tears down
            ret = self._ffmpeg_proc.poll()
            if ret is not None:
                ff_err = ""
                try:
                    ff_err = self._ffmpeg_proc.stderr.read().decode(errors="replace")
                except Exception:
                    pass
                logger.error(
                    "Music stream start failed (ffmpeg rc=%s): %s | yt-dlp: %s | aplay: %s",
                    ret, ff_err[-300:], self._read_ytdlp_stderr()[-400:],
                    self._read_aplay_stderr()[-300:],
                )
                return False
            time.sleep(0.1)
        return True

    def _resolve_audio_url(self, query: str) -> tuple[Optional[str], Optional[str]]:
        """Use yt-dlp to search YouTube and return (watch_url, title).

        Retries on transient failure (YouTube throttling / bot-detection) — a single
        non-zero exit isn't a dead video. Gives up after MUSIC_RESOLVE_TRIES."""
        last_err = ""
        for attempt in range(MUSIC_RESOLVE_TRIES):
            if attempt > 0:
                time.sleep(MUSIC_RETRY_BACKOFF_S)
                logger.info("yt-dlp resolve retry %d/%d: '%s'",
                            attempt + 1, MUSIC_RESOLVE_TRIES, query[:60])
            try:
                result = subprocess.run(
                    [
                        sys.executable, "-m", "yt_dlp",
                        "--js-runtimes", "node:/usr/bin/node",
                        "--remote-components", "ejs:github",
                        "--dump-json",
                        "--no-download",
                        f"ytsearch1:{query}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=90,
                )

                if result.returncode != 0:
                    last_err = (result.stderr or "").strip() or f"exit {result.returncode}"
                    logger.warning("yt-dlp resolve attempt %d failed: %s",
                                   attempt + 1, last_err[:200])
                    continue

                info = json.loads(result.stdout)
                title = info.get("title", query)
                watch_url = info.get("webpage_url")

                if not watch_url:
                    last_err = "no webpage_url in result"
                    logger.warning("yt-dlp returned no URL for: '%s'", query[:80])
                    continue

                logger.info("Found: '%s' (%s)", title, watch_url)
                return watch_url, title

            except subprocess.TimeoutExpired:
                last_err = "timed out"
                logger.warning("yt-dlp resolve attempt %d timed out", attempt + 1)
                continue
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.warning("yt-dlp resolve attempt %d errored: %s", attempt + 1, last_err)
                continue

        logger.error("yt-dlp resolve failed after %d tries for '%s': %s",
                     MUSIC_RESOLVE_TRIES, query[:80], last_err[:200])
        return None, None
