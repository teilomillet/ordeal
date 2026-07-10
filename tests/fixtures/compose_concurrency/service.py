"""Two-role fan-out fixture that proves concurrent backend execution."""

from __future__ import annotations

import json
import os
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROLE = os.environ["ORDEAL_ROLE"]
FANOUT = 8
_barrier = threading.Barrier(FANOUT)
_lock = threading.Lock()
_active = 0
_max_active = 0


def _json(handler: BaseHTTPRequestHandler, payload: dict[str, object]) -> None:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _worker_call(index: int) -> dict[str, object]:
    with urllib.request.urlopen(f"http://worker:8080/unit?id={index}", timeout=3) as response:
        return json.loads(response.read())


class Handler(BaseHTTPRequestHandler):
    """Serve worker synchronization and API fan-out endpoints."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        global _active, _max_active

        if self.path == "/health":
            _json(self, {"status": "ok", "role": ROLE})
            return
        if ROLE == "worker" and self.path.startswith("/unit?"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            identifier = int(query["id"][0])
            with _lock:
                _active += 1
                _max_active = max(_max_active, _active)
            try:
                _barrier.wait(timeout=2)
                _json(self, {"id": identifier, "max_active": _max_active})
            finally:
                with _lock:
                    _active -= 1
            return
        if ROLE == "api" and self.path == "/fanout":
            with ThreadPoolExecutor(max_workers=FANOUT) as pool:
                results = list(pool.map(_worker_call, range(FANOUT)))
            _json(
                self,
                {
                    "service": "fanout-api",
                    "unique": len({int(item["id"]) for item in results}),
                    "max_concurrency": max(int(item["max_active"]) for item in results),
                },
            )
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress routine request logs."""


def main() -> None:
    """Run one role until Compose stops the container."""
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
