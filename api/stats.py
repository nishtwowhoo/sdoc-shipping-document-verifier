"""GET /api/stats — dashboard counters JSON."""
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
            sdoc.send_json(self, sdoc.get_app().stats)
        except Exception as exc:  # noqa: BLE001
            sdoc.send_err(self, 500, f"internal error: {exc}")
