"""GET / — dashboard."""
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
            theme = sdoc.pick_theme(self, q)
            sdoc.send_html(self, theme, "Dashboard", theme.render_index(app))
        except Exception as exc:  # noqa: BLE001
            sdoc.send_err(self, 500, f"internal error: {exc}")
