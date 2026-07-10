"""Two-role HTTP fixture for API-restart persistence evidence."""

from __future__ import annotations

import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROLE = os.environ["ORDEAL_ROLE"]
STORE_URL = "http://store:8080/item"


def _json(handler: BaseHTTPRequestHandler, payload: dict[str, object]) -> None:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    """Expose a durable store and a separate proxy API."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path == "/health":
            _json(self, {"status": "ok", "role": ROLE})
            return
        if self.path != "/item":
            self.send_error(404)
            return
        if ROLE == "store":
            _json(self, {"id": "item-1", "value": "committed", "backend": "store"})
            return
        with urllib.request.urlopen(STORE_URL, timeout=2) as response:
            stored = json.loads(response.read())
        _json(self, {**stored, "service": "api"})

    def log_message(self, format: str, *args: object) -> None:
        """Suppress routine request logs."""


def main() -> None:
    """Run one role until Compose stops the container."""
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
