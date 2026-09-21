"""GET /api/score — aggregate score JSON (baked into snapshot at build)."""
from http.server import BaseHTTPRequestHandler
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from _lib import sdoc


class handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        try:
            sdoc.send_json(self, sdoc.get_app().score or {"error": "no score in snapshot"})
        except Exception as exc:  # noqa: BLE001
            sdoc.send_err(self, 500, f"internal error: {exc}")
