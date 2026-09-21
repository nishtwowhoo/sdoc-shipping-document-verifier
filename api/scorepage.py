"""GET /score — scorecard page (score baked into snapshot at build)."""
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
            app = sdoc.get_app()
            if not app.score:
                return sdoc.send_err(self, 404, "no score in snapshot")
            theme = sdoc.pick_theme(self, q)
            sdoc.send_html(self, theme, "Score", theme.render_score(app))
        except Exception as exc:  # noqa: BLE001
            sdoc.send_err(self, 500, f"internal error: {exc}")
