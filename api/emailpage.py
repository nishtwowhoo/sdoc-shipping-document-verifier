"""GET /email?eid=<id> — email detail page (rewritten from /email/<id>)."""
from http.server import BaseHTTPRequestHandler
import os
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(__file__))

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
            theme = sdoc.pick_theme(self, q)
            sdoc.send_html(self, theme, eid, theme.render_email(app, eid))
        except Exception as exc:  # noqa: BLE001
            sdoc.send_err(self, 500, f"internal error: {exc}")
