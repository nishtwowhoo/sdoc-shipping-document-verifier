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
            snap = sdoc.snapshot()
            meta = (snap.get("meta") or {}) if isinstance(snap, dict) else {}
            sdoc.send_json(self, {"backend": app.store.backend, "config": {
                "supabase": bool(app.store.cfg.get("configured")),
                "gemini": bool(os.environ.get("GEMINI_API_KEY")),
            }, "deploy": {
                "commit": os.environ.get("VERCEL_GIT_COMMIT_SHA", ""),
                "snapshot_count": meta.get("count"),
                "snapshot_at": meta.get("generated_at"),
                "rows": len(app.rows),
            }})
        except Exception as exc:  # noqa: BLE001
            sdoc.send_err(self, 500, f"internal error: {exc}")
