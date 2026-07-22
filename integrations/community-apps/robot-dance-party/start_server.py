"""Simple CORS-enabled static file server for HF Spaces.

Identical pattern to reachy-dance-duo's start_server.py:
serves index.html + JS/CSS with SharedArrayBuffer headers
(needed for Web Audio API in some browsers).
"""

import http.server
import sys


class CORSHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    with http.server.HTTPServer(("", port), CORSHandler) as server:
        print(f"Serving on http://localhost:{port}")
        server.serve_forever()
