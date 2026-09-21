"""Shared serverless backend: snapshot-backed App + HTTP helpers.

Serves the committed snapshot (api/_snapshot/snapshot.json) with live HITL
overrides from Supabase. No attachment parsing at request time (fast enough
for serverless timeouts). Local fallback files are directed at /tmp so the
read-only Vercel filesystem is never written.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = API_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import webui  # noqa: E402
import hitl  # noqa: E402
import actions  # noqa: E402

SNAPSHOT_PATH = API_DIR.parent / "_snapshot" / "snapshot.json"
REQUIRE_SUPABASE = os.environ.get("HITL_REQUIRE_SUPABASE", "") == "1"

_snapshot_cache = None


def snapshot() -> dict:
    global _snapshot_cache
    if _snapshot_cache is None:
        _snapshot_cache = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    return _snapshot_cache


class SnapshotApp(webui.App):
    """webui.App interface backed by the committed snapshot (no _build)."""

    def __init__(self):
        snap = snapshot()
        self.data_dir = "snapshot"
        self.base_rows = copy.deepcopy(snap["rows"])
        self.details = copy.deepcopy(snap["details"])
        self.inbox = None
        self.store = hitl.Store("/tmp")
        self.action_store = actions.ActionStore("/tmp")
        self._explain_cache: dict = {}
        self.rows = copy.deepcopy(self.base_rows)
        self._apply_overrides()
        for r in self.rows:
            r["ui"] = webui._ui_metrics(r, self.details[r["email_id"]])
        self.sub = {
            r["email_id"]: {
                k: r[k]
                for k in ("category", "status", "review_reason", "defect_fields", "has_defect")
            }
            for r in self.rows
        }
        self.score = copy.deepcopy(snap.get("score"))

    def _refresh_row(self, email_id: str) -> None:
        fresh = copy.deepcopy(next(
            r for r in self.base_rows if r["email_id"] == email_id))
        ov = self.overrides.get(email_id)
        if ov:
            if ov.get("resolved_category"):
                fresh["category"] = ov["resolved_category"]
            if ov.get("resolved_status"):
                fresh["status"] = ov["resolved_status"]
                if ov["resolved_status"] != "NEEDS_REVIEW":
                    fresh["review_reason"] = None
        fresh["ui"] = webui._ui_metrics(fresh, self.details[email_id])
        for i, r in enumerate(self.rows):
            if r["email_id"] == email_id:
                self.rows[i] = fresh
                break
        self.sub[email_id] = {
            k: fresh[k]
            for k in ("category", "status", "review_reason", "defect_fields", "has_defect")
        }

    def reset_all(self) -> dict:
        saved = self.store.clear()
        self.overrides = {}
        self._explain_cache = {}
        self.rows = copy.deepcopy(self.base_rows)
        for r in self.rows:
            r["ui"] = webui._ui_metrics(r, self.details[r["email_id"]])
        self.sub = {
            r["email_id"]: {
                k: r[k]
                for k in ("category", "status", "review_reason", "defect_fields", "has_defect")
            }
            for r in self.rows
        }
        return saved


def get_app() -> SnapshotApp:
    """Fresh app per request so Supabase overrides are always current."""
    return SnapshotApp()


def enforce_supabase(saved: dict) -> None:
    """Fail loud when serverless triage can't persist (no silent /tmp writes)."""
    if REQUIRE_SUPABASE and "supabase" not in str(saved.get("backend", "")):
        raise RuntimeError(
            "review storage unavailable (Supabase not configured or write failed)")


# -- HTTP helpers (mirror Handler methods) -----------------------------------
def send(handler, code: int, body: bytes, ctype: str) -> None:
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    cookie = getattr(handler, "_pending_cookie", None)
    if cookie:
        handler.send_header("Set-Cookie", cookie)
    handler.end_headers()
    handler.wfile.write(body)


def send_json(handler, obj, code: int = 200) -> None:
    send(handler, code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
         "application/json; charset=utf-8")


def send_err(handler, code: int, msg: str) -> None:
    send(handler, code, msg.encode(), "text/plain; charset=utf-8")


def send_html(handler, theme, title: str, body: str) -> None:
    send(handler, 200, theme.shell(title, body), "text/html; charset=utf-8")


def pick_theme(handler, q) -> object:
    themes = webui.themes
    q_theme = (q.get("theme") or [""])[0]
    if q_theme in themes:
        handler._pending_cookie = (
            f"sdoc_theme={q_theme}; Path=/; Max-Age=31536000; SameSite=Lax")
        return themes[q_theme]
    raw = handler.headers.get("Cookie", "")
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "sdoc_theme" and v in themes:
            return themes[v]
    return themes[webui.default_theme]


def read_json_body(handler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
    except ValueError:
        length = 0
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {}
