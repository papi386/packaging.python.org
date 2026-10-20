"""Fork-only deterministic endpoints. Never include this in the upstream patch."""

import json
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REQUESTS = Counter()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, body=False):
        REQUESTS[self.path] += 1
        if self.path == "/timeout":
            time.sleep(4)
        status = 404 if self.path in {"/old-broken", "/new-broken", "/prefix/broken"} else 200
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if body:
            try:
                self.wfile.write(b'<html><body><h1 id="present">Fixture</h1></body></html>')
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_HEAD(self):
        self.respond()

    def do_GET(self):
        self.respond(body=True)


def start(app):
    if app.builder.name != "linkcheck":
        return
    app._validation_server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    threading.Thread(target=app._validation_server.serve_forever, daemon=True).start()


def finish(app, exception):
    if app.builder.name != "linkcheck":
        return
    report = Path(app.outdir) / "output.json"
    records = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()] if report.exists() else []
    local = [r for r in records if r.get("uri", "").startswith("http://127.0.0.1:8765/")]
    print("CI_VALIDATION_REPORT=" + json.dumps({
        "statuses": dict(Counter(r["status"] for r in local)),
        "requests": dict(REQUESTS),
        "error": type(exception).__name__ if exception else None,
    }, sort_keys=True))
    server = getattr(app, "_validation_server", None)
    if server:
        server.shutdown()
        server.server_close()


def setup(app):
    app.connect("builder-inited", start)
    app.connect("build-finished", finish)
    return {"parallel_read_safe": True}
