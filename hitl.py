"""HITL AI + Supabase backend for webui.py (stdlib only, no new pip deps).

Env vars (also read from .env in project root):
  GEMINI_API_KEY - Google Gemini key (chatbox + pipeline classification)
  GEMINI_MODEL        - default: gemini-3.6-flash (override without code change)
  SUPABASE_URL        - e.g. https://xyz.supabase.co
  SUPABASE_KEY        - anon or service_role key (also accepts
                        SUPABASE_ANON_KEY / SUPABASE_SERVICE_KEY)
  SUPABASE_TABLE      - default: hitl_reviews
  HITL_LOCAL_FILE     - local fallback file, default: hitl_overrides.json

Supabase table schema (run once in SQL editor):
    create table if not exists hitl_reviews (
      email_id text primary key,
      orig_category text,
      orig_status text,
      review_reason text,
      resolved_category text,
      resolved_status text,
      ai_explanation text,
      ai_fix text,
      resolved_by text default 'human',
      note text default '',
      resolved_at timestamptz default now(),
      updated_at timestamptz default now()
    );
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time
import urllib.request
import urllib.error
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    try:
        p = Path(path)
        if not p.is_file():
            return
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k and k not in os.environ:
                os.environ[k] = v.strip().strip("'\"")
    except Exception:
        pass


load_dotenv()

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

def _chatbox_key() -> str:
    """Single project key for chatbox (and pipeline classification)."""
    return os.environ.get("GEMINI_API_KEY", "")


def _gemini_post(prompt: str, json_mode: bool) -> str:
    """POST to Gemini with retries on transient failures.

    Retries 5xx/timeouts with backoff, and 429 honoring the server's
    "retry in Xs" delay (free-tier quota). Non-retryable errors
    (bad key, unknown model) raise immediately.
    """
    import re

    key = _chatbox_key()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={key}"
    )
    body: dict = {"contents": [{"parts": [{"text": prompt}]}]}
    if json_mode:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    payload = json.dumps(body).encode()
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429:
                try:
                    detail = exc.read().decode("utf-8", "replace")
                except Exception:
                    detail = ""
                m = re.search(r"retry in ([\d.]+)s", detail)
                wait = min(float(m.group(1)) + 1, 65) if m else 30
                exc._retry_after = wait  # stash for the error message
                exc._detail = detail[:300]
                time.sleep(wait)
                continue
            if exc.code is None or not (500 <= exc.code < 600):
                raise  # auth / bad request / not found: retrying won't help
        except Exception as exc:  # timeouts, connection resets: transient
            last_exc = exc
        time.sleep(2 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def describe_error(exc: Exception) -> str:
    """Human-friendly one-liner for a Gemini failure."""
    import re

    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
        wait = getattr(exc, "_retry_after", None)
        hint = f" Retry in ~{wait:.0f}s." if wait else ""
        return ("AI rate-limited (free-tier quota, ~20 requests). "
                f"Wait a minute and ask again.{hint}")
    return f"AI ran into an error: {exc}"


def _email_context(email: dict, docs: list, review_reason: str | None) -> str:
    docs_txt = []
    for d in (docs or [])[:3]:
        docs_txt.append(
            f"- {d.get('path')}: kind={d.get('kind')} "
            f"readable={d.get('readable')} "
            f"text={(d.get('text') or '')[:1200]}"
        )
    return (
        f"email_id: {email.get('email_id')}\n"
        f"subject: {email.get('subject')}\n"
        f"from: {email.get('from')}\n"
        f"review_reason: {review_reason}\n"
        f"body (first 2000 chars): {(email.get('body') or '')[:2000]}\n"
        "attachments:\n" + "\n".join(docs_txt)
    )


PLEASANTRIES = re.compile(
    r"^(thanks?|thank\s*you|thx|ok(ay)?|got\s*it|noted|great|perfect|"
    r"understood|cool|awesome|nice|cheers)[\s.!]*$",
    re.IGNORECASE,
)


def gemini_explain(email: dict, docs: list, review_reason: str | None,
                   question: str | None = None) -> dict:
    """Real Gemini chatbox. No rule-based answers; errors are explicit.

    First call (no question) returns structured JSON; follow-ups return
    free-text answers. Single-shot: each call carries full email context.
    Pure pleasantries are answered locally (no quota burned).
    """
    if not _chatbox_key():
        return {
            "error": "unavailable",
            "explanation": ("AI unavailable: GEMINI_API_KEY is not set. "
                            "Add it to your .env file or pass -e GEMINI_API_KEY=... to docker run."),
            "source": "error",
        }
    try:
        ctx = _email_context(email, docs, review_reason)
        if question:
            if PLEASANTRIES.match(question.strip()):
                return {
                    "answer": ("You're welcome! If you've got what you need, "
                               "pick the corrected category above and hit Resolve — "
                               "or ask me anything else about this email."),
                    "review_reason": review_reason,
                    "source": "local",
                }
            prompt = (
                "You are a shipping-document HITL assistant. An email was flagged NEEDS_REVIEW.\n"
                + ctx + "\n\n"
                f"The human reviewer asks: {question}\n\n"
                "Answer directly in plain text (no JSON), using the email context above. "
                "Be concise and specific to this email. "
                "If the message is just a pleasantry or acknowledgment (e.g. thanks, ok), "
                "reply briefly and warmly WITHOUT re-explaining the whole case."
            )
            text = _gemini_post(prompt, json_mode=False)
            return {
                "answer": text[:2000],
                "review_reason": review_reason,
                "source": "gemini",
            }
        prompt = (
            "You are a shipping-document HITL assistant. An email was flagged NEEDS_REVIEW.\n"
            + ctx + "\n\n"
            "Reply ONLY as JSON with keys: explanation (2-4 sentences, plain language), "
            "suggested_fix (1-3 concrete next steps), "
            "suggested_category (one of BL_COMPARISON, SI_REQUEST, INVOICE_QUERY, GENERAL, SPAM), "
            "suggested_status (one of OK, MISMATCH, NEEDS_REVIEW)."
        )
        text = _gemini_post(prompt, json_mode=True)
        parsed = json.loads(text)
        return {
            "explanation": str(parsed.get("explanation", ""))[:2000],
            "suggested_fix": str(parsed.get("suggested_fix", ""))[:2000],
            "suggested_category": str(parsed.get("suggested_category", "GENERAL")),
            "suggested_status": str(parsed.get("suggested_status", "OK")),
            "review_reason": review_reason,
            "source": "gemini",
        }
    except Exception as exc:  # quota / network / bad key / bad response
        return {
            "error": "gemini_error",
            "explanation": describe_error(exc),
            "review_reason": review_reason,
            "source": "error",
        }


# ---------------------------------------------------------------------------
# Supabase store (REST) with local JSON fallback
# ---------------------------------------------------------------------------

def supabase_cfg() -> dict:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = (
        os.environ.get("SUPABASE_KEY", "")
        or os.environ.get("SUPABASE_ANON_KEY", "")
        or os.environ.get("SUPABASE_SERVICE_KEY", "")
    )
    return {
        "url": url,
        "key": key,
        "table": os.environ.get("SUPABASE_TABLE", "hitl_reviews"),
        "configured": bool(url and key),
    }


class Store:
    """Persist HITL resolutions. Supabase when configured, else local JSON."""

    def __init__(self, data_dir: str = "."):
        self.data_dir = data_dir
        self.cfg = supabase_cfg()
        fname = os.environ.get("HITL_LOCAL_FILE", "hitl_overrides.json")
        p = Path(fname)
        self.local_path = p if p.is_absolute() else Path(data_dir) / p.name
        if self.local_path.is_file():
            try:
                self._local = json.loads(self.local_path.read_text(encoding="utf-8"))
            except Exception:
                self._local = {}
        else:
            self._local = {}

    @property
    def backend(self) -> str:
        return "supabase" if self.cfg["configured"] else "local"

    # -- local fallback -------------------------------------------------
    def _save_local(self) -> None:
        try:
            if not self._local:
                self.local_path.unlink(missing_ok=True)
            else:
                self.local_path.write_text(json.dumps(self._local, indent=2), encoding="utf-8")
        except Exception:
            pass

    # -- REST helpers ---------------------------------------------------
    def _req(self, method: str, path: str, body: dict | None = None):
        url = f"{self.cfg['url']}/rest/v1/{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "apikey": self.cfg["key"],
            "Authorization": f"Bearer {self.cfg['key']}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else None

    def all(self) -> dict:
        if not self.cfg["configured"]:
            return dict(self._local)
        try:
            rows = self._req("GET", f"{self.cfg['table']}?select=*")
            return {r["email_id"]: r for r in (rows or []) if r.get("email_id")}
        except Exception:
            return dict(self._local)

    def get(self, email_id: str) -> dict | None:
        if not self.cfg["configured"]:
            return self._local.get(email_id)
        try:
            rows = self._req("GET", f"{self.cfg['table']}?email_id=eq.{email_id}&select=*")
            return rows[0] if rows else self._local.get(email_id)
        except Exception:
            return self._local.get(email_id)

    def save(self, record: dict) -> dict:
        record = dict(record)
        record["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if "resolved_at" not in record:
            record["resolved_at"] = record["updated_at"]
        self._local[record["email_id"]] = record
        self._save_local()
        if not self.cfg["configured"]:
            return {"backend": "local", "record": record}
        try:
            self._req("POST", self.cfg["table"], record)
            return {"backend": "supabase", "record": record}
        except (urllib.error.HTTPError, urllib.error.URLError, Exception) as exc:
            return {"backend": "local (supabase failed)", "record": record, "error": str(exc)}

    def delete(self, email_id: str) -> dict:
        self._local.pop(email_id, None)
        self._save_local()
        if not self.cfg["configured"]:
            return {"backend": "local"}
        try:
            self._req("DELETE", f"{self.cfg['table']}?email_id=eq.{email_id}")
            return {"backend": "supabase"}
        except Exception as exc:
            return {"backend": "local (supabase failed)", "error": str(exc)}

    def clear(self) -> dict:
        self._local = {}
        self._save_local()
        if not self.cfg["configured"]:
            return {"backend": "local", "cleared": True}
        try:
            # delete all rows (supabase requires a filter; email_id not null matches all)
            self._req("DELETE", f"{self.cfg['table']}?email_id=not.is.null")
            return {"backend": "supabase", "cleared": True}
        except Exception as exc:
            return {"backend": "local (supabase failed)", "error": str(exc)}
