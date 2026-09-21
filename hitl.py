"""HITL AI + Supabase backend for webui.py (stdlib only, no new pip deps).

Env vars (also read from .env in project root):
  GEMINI_API_KEY      - Google Gemini key for AI explanations
  GEMINI_MODEL        - default: gemini-1.5-flash
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

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")

FALLBACK = {
    "missing_attachment": {
        "explanation": (
            "A BL comparison was requested but no draft Bill of Lading "
            "attachment was found. The pipeline escalated this instead of "
            "guessing, so a person must confirm whether the BL is truly "
            "missing or was sent separately."
        ),
        "fix": "Ask the shipper/forwarder to re-send the draft BL, then re-run. If the email was never a comparison request, re-categorize it (e.g. SI_REQUEST or GENERAL) and mark OK.",
        "suggested_category": "BL_COMPARISON",
        "suggested_status": "OK",
    },
    "wrong_doc_type": {
        "explanation": (
            "An attachment was present but it is not a Bill of Lading "
            "(e.g. Commercial Invoice, Packing List, or Certificate of Origin). "
            "Comparing it against the Shipping Instruction would be meaningless, "
            "so human review is required."
        ),
        "fix": "Request the correct draft BL document. If no BL comparison is actually needed, re-categorize to the true intent (often GENERAL) and mark OK.",
        "suggested_category": "BL_COMPARISON",
        "suggested_status": "OK",
    },
    "unreadable": {
        "explanation": (
            "At least one attachment could not be read (corrupt, scanned image "
            "without text, empty file, or unsupported encoding). The extractor "
            "refused to compare garbled content."
        ),
        "fix": "Ask for a re-upload as searchable PDF/TXT/DOCX. If the file opens fine for you, note that in the review and resolve manually.",
        "suggested_category": "BL_COMPARISON",
        "suggested_status": "OK",
    },
    "missing_value": {
        "explanation": (
            "The Shipping Instruction contains placeholder values such as ???, "
            "_______, TBA or N/A. The pipeline will not sign off on incomplete "
            "source data."
        ),
        "fix": "Get the completed SI with all 7 fields filled (shipper, consignee, notify_party, POL, POD, container_count, gross_weight_kg), then resolve as BL_COMPARISON / OK or MISMATCH once compared.",
        "suggested_category": "BL_COMPARISON",
        "suggested_status": "OK",
    },
}


def fallback_explain(review_reason: str | None) -> dict:
    fb = FALLBACK.get(review_reason or "", {
        "explanation": "This email needs human review before the pipeline can close it.",
        "fix": "Read the body + attachments, pick the correct category, and resolve.",
        "suggested_category": "GENERAL",
        "suggested_status": "OK",
    })
    return {
        "explanation": fb["explanation"],
        "suggested_fix": fb["fix"],
        "suggested_category": fb["suggested_category"],
        "suggested_status": fb["suggested_status"],
        "source": "fallback",
    }


def _gemini_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")


def gemini_explain(email: dict, docs: list, review_reason: str | None) -> dict:
    """Ask Gemini what's wrong; fall back to rules when no key/offline."""
    fb = fallback_explain(review_reason)
    key = _gemini_key()
    if not key:
        return fb
    try:
        docs_txt = []
        for d in (docs or [])[:3]:
            docs_txt.append(
                f"- {d.get('path')}: kind={d.get('kind')} "
                f"readable={d.get('readable')} "
                f"text={(d.get('text') or '')[:1200]}"
            )
        prompt = (
            "You are a shipping-document HITL assistant. An email was flagged NEEDS_REVIEW.\n"
            f"email_id: {email.get('email_id')}\n"
            f"subject: {email.get('subject')}\n"
            f"from: {email.get('from')}\n"
            f"review_reason: {review_reason}\n"
            f"body (first 2000 chars): {(email.get('body') or '')[:2000]}\n"
            "attachments:\n" + "\n".join(docs_txt) + "\n\n"
            "Reply ONLY as JSON with keys: explanation (2-4 sentences, plain language), "
            "suggested_fix (1-3 concrete next steps), "
            "suggested_category (one of BL_COMPARISON, SI_REQUEST, INVOICE_QUERY, GENERAL, SPAM), "
            "suggested_status (one of OK, MISMATCH, NEEDS_REVIEW)."
        )
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={key}"
        )
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        try:
            parsed = json.loads(text)
        except Exception:
            # model returned prose despite mime request; wrap it
            return {
                "explanation": text[:1500],
                "suggested_fix": fb["suggested_fix"],
                "suggested_category": fb["suggested_category"],
                "suggested_status": fb["suggested_status"],
                "source": "gemini",
            }
        return {
            "explanation": str(parsed.get("explanation", fb["explanation"]))[:2000],
            "suggested_fix": str(parsed.get("suggested_fix", fb["suggested_fix"]))[:2000],
            "suggested_category": str(parsed.get("suggested_category", fb["suggested_category"])),
            "suggested_status": str(parsed.get("suggested_status", fb["suggested_status"])),
            "source": "gemini",
        }
    except Exception as exc:  # offline / quota / bad key -> rules
        fb = fallback_explain(review_reason)
        fb["source"] = "fallback"
        fb["note"] = f"gemini unavailable: {exc}"
        return fb


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
