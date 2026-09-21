"""GET /api/review?eid=<id> — HITL review state; POST /api/review — resolve."""
from http.server import BaseHTTPRequestHandler
import os
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(__file__))

import webui
from _lib import sdoc


class handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        try:
            q = parse_qs(urlparse(self.path).query)
            eid = (q.get("eid") or [""])[0]
            app = sdoc.get_app()
            if not eid or eid not in app.details:
                return sdoc.send_err(self, 404, "not found")
            sdoc.send_json(self, app.store.get(eid) or {"email_id": eid, "resolved": False})
        except Exception as exc:  # noqa: BLE001
            sdoc.send_err(self, 500, f"internal error: {exc}")

    def do_POST(self):
        try:
            payload = sdoc.read_json_body(self)
            app = sdoc.get_app()
            eid = payload.get("email_id", "")
            if eid not in app.details:
                return sdoc.send_json(self, {"error": "unknown email_id"}, 404)
            if payload.get("resolved_category") not in webui.CATEGORIES:
                return sdoc.send_json(self, {"error": "bad resolved_category"}, 400)
            if payload.get("resolved_status") not in webui.STATUSES:
                return sdoc.send_json(self, {"error": "bad resolved_status"}, 400)
            saved = app.resolve(eid, payload)
            try:
                sdoc.enforce_supabase(saved)
            except RuntimeError as exc:
                return sdoc.send_json(self, {"error": str(exc)}, 502)
            return sdoc.send_json(self, {"ok": True, **saved})
        except Exception as exc:  # noqa: BLE001
            sdoc.send_err(self, 500, f"internal error: {exc}")
