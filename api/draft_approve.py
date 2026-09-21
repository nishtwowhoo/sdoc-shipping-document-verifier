"""POST /api/draft/approve — log an approved reply draft."""
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
            payload = sdoc.read_json_body(self)
            app = sdoc.get_app()
            eid = payload.get("email_id", "")
            if eid not in app.details:
                return sdoc.send_json(self, {"error": "unknown email_id"}, 404)
            saved = app.approve_draft(payload)
            try:
                sdoc.enforce_supabase(saved)
            except RuntimeError as exc:
                return sdoc.send_json(self, {"error": str(exc)}, 502)
            return sdoc.send_json(self, {"ok": True, **saved})
        except Exception as exc:  # noqa: BLE001
            sdoc.send_err(self, 500, f"internal error: {exc}")
