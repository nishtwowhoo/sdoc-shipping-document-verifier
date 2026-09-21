"""GET /api/ping — minimal routing probe (no snapshot, no Supabase).

If this returns JSON, per-file function routing works and the problem is
specific to the other endpoints. If it returns the dashboard HTML, the
project has a catch-all rewrite/fallback in its live Vercel config.
"""
from http.server import BaseHTTPRequestHandler
import json
import os


class handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        body = json.dumps({
            "ok": True,
            "commit": os.environ.get("VERCEL_GIT_COMMIT_SHA", ""),
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
