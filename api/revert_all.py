"""POST /api/revert-all — clear all HITL resolutions."""
from http.server import BaseHTTPRequestHandler
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from _lib import sdoc


class handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        try:
            saved = sdoc.get_app().reset_all()
            try:
                sdoc.enforce_supabase(saved)
            except RuntimeError as exc:
                return sdoc.send_json(self, {"error": str(exc)}, 502)
            return sdoc.send_json(self, {"ok": True, **saved})
        except Exception as exc:  # noqa: BLE001
            sdoc.send_err(self, 500, f"internal error: {exc}")
