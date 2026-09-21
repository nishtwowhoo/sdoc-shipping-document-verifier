"""GET /api/hitl/status — backend + config flags JSON."""
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
            app = sdoc.get_app()
            sdoc.send_json(self, {"backend": app.store.backend, "config": {
                "supabase": bool(app.store.cfg.get("configured")),
                "gemini": bool(os.environ.get("GEMINI_API_KEY")),
            }})
        except Exception as exc:  # noqa: BLE001
            sdoc.send_err(self, 500, f"internal error: {exc}")
