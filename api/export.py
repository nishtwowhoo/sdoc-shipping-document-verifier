"""GET /export — CSV download of effective rows."""
from http.server import BaseHTTPRequestHandler
import csv
import io
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
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["email_id", "category", "status", "has_defect", "review_reason",
                        "defect_fields", "from", "subject"])
            for r in app.rows:
                w.writerow([r["email_id"], r["category"], r["status"], r["has_defect"],
                            r["review_reason"] or "", "|".join(r["defect_fields"]),
                            r["from"], r["subject"]])
            data = buf.getvalue().encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="emails.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:  # noqa: BLE001
            sdoc.send_err(self, 500, f"internal error: {exc}")
