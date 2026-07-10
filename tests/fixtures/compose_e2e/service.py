"""Dependency-free HTTP service for the real Docker Compose CI gate."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VARIANT = os.environ.get("ORDEAL_SERVICE_VARIANT", "fixed")
if VARIANT not in {"buggy", "fixed"}:
    raise ValueError("ORDEAL_SERVICE_VARIANT must be 'buggy' or 'fixed'")

RESTART_MARKER = Path("/tmp/ordeal-compose-e2e-started")
RESTARTED = RESTART_MARKER.exists()
RESTART_MARKER.touch()


def recovery_status() -> str:
    """Return the post-restart state exposed by each checked-in variant."""
    if VARIANT == "buggy" and RESTARTED:
        return "degraded"
    return "ok"


class Handler(BaseHTTPRequestHandler):
    """Serve deterministic health and state responses."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        """Return JSON for the two endpoints exercised by the runner."""
        if self.path not in {"/health", "/state"}:
            self.send_error(404)
            return
        status = "ok" if self.path == "/health" else recovery_status()
        body = json.dumps(
            {"service": "compose-e2e", "status": status, "variant": VARIANT},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep routine request logs out of the CI output."""


def main() -> None:
    """Run the fixture service until Docker stops the container."""
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
