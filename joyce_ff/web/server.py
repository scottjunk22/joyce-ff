"""
Serve the draft board as a local web page.

No framework, no external hosting: stdlib http.server bound to localhost. The
board data is computed once (or loaded from the on-disk cache) and injected
into a single self-contained HTML file, so the page needs no network at all —
it keeps working if the internet dies mid-draft.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..projections import board as board_mod

TEMPLATE = Path(__file__).resolve().parent / "templates" / "board.html"


def _render(data: dict) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    # json.dumps is safe to inline; escape '<' so a name can never break out.
    payload = json.dumps(data).replace("<", "\\u003c")
    return html.replace("__BOARD_DATA__", payload)


def _make_handler(page: str, data: dict):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(page.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/data.json":
                self._send(json.dumps(data).encode("utf-8"),
                           "application/json; charset=utf-8")
            else:
                self.send_error(404)

        def log_message(self, *a):  # keep the console quiet
            pass

    return Handler


def serve(data: dict, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True) -> None:
    page = _render(data)
    httpd = ThreadingHTTPServer((host, port), _make_handler(page, data))
    url = f"http://{host}:{port}/"
    print(f"Draft board live at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
        httpd.shutdown()


def run(argv=None) -> int:
    argv = argv or []
    refresh = "--refresh" in argv
    no_browser = "--no-browser" in argv
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8765

    data = None if refresh else board_mod.load_cached()
    if data is None:
        print("Building draft boards (first run or --refresh)...")
        data = board_mod.build_and_cache()
    else:
        print(f"Loaded cached boards from {data['generated_at']} "
              f"(use --refresh to rebuild).")

    serve(data, port=port, open_browser=not no_browser)
    return 0
