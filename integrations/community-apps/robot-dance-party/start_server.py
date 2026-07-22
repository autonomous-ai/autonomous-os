"""Static file server + YouTube audio download.

Auto-installs yt-dlp if missing. User just runs: python start_server.py
"""

import http.server
import json
import subprocess
import sys
from pathlib import Path

# Auto-install yt-dlp if missing
try:
    import yt_dlp
except ImportError:
    print("Installing yt-dlp...")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "yt-dlp", "-q",
        "--break-system-packages",
    ])
    import yt_dlp

import tempfile
DOWNLOADS = Path(tempfile.gettempdir()) / "robot-dance-party"
DOWNLOADS.mkdir(exist_ok=True)


def download_audio(url):
    """Download YouTube audio as mp3, return (filepath, title)."""
    info = {"title": "Unknown"}

    def _progress(d):
        nonlocal info
        if d.get("info_dict"):
            info = d["info_dict"]

    opts = {
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        "outtmpl": str(DOWNLOADS / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "progress_hooks": [_progress],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        result = ydl.extract_info(url, download=True)
        video_id = result.get("id", "unknown")
        title = result.get("title", "Unknown")

    mp3 = DOWNLOADS / f"{video_id}.mp3"
    if not mp3.exists():
        for f in DOWNLOADS.glob(f"{video_id}.*"):
            mp3 = f
            break
    return mp3, title


class AppHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "credentialless")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/yt/download":
            self._handle_download()
        else:
            self.send_error(404)

    def _handle_download(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            url = body.get("url", "").strip()
            if not url:
                return self._json(400, {"error": "url required"})

            mp3, title = download_audio(url)
            self._json(200, {
                "audio_url": f"/downloads/{mp3.name}",
                "title": title,
            })
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"Robot Dance Party — http://localhost:{port}")
    with http.server.HTTPServer(("", port), AppHandler) as s:
        s.serve_forever()
