"""HITL Action Engine: one-click reply draft generator (stdlib only).

For flagged emails (MISMATCH / NEEDS_REVIEW) this builds a strict,
deterministic reply draft from the pipeline's own facts — no LLM, no quota.

Supabase table (run once in SQL editor; optional — falls back to local file):
    create table if not exists hitl_actions (
      id bigserial primary key,
      email_id text,
      action text,
      draft_to text,
      draft_subject text,
      draft_body text,
      approved_by text default 'human',
      created_at timestamptz default now()
    );
"""

from __future__ import annotations

import datetime
import json
import urllib.request
from pathlib import Path

import hitl

FIELD_LABELS = {
    "shipper": "Shipper",
    "consignee": "Consignee",
    "notify_party": "Notify party",
    "port_of_loading": "Port of loading",
    "port_of_discharge": "Port of discharge",
    "container_count": "Container count",
    "gross_weight_kg": "Gross weight",
}


def _fmt(v) -> str:
    v = "" if v is None else str(v).strip()
    return v if v else "— not stated —"


def _si_bl_docs(docs: list) -> tuple:
    si = next((d for d in docs if d.get("kind") == "shipping_instruction"), None)
    bl = next((d for d in docs if d.get("kind") == "bill_of_lading"), None)
    return si, bl


def _doc_names(docs: list) -> str:
    names = [d.get("path", "") for d in (docs or []) if d.get("path")]
    return ", ".join(names) if names else "the attached documents"


def build_draft(row: dict, detail: dict) -> dict:
    """Build To/Subject/Body draft for a flagged email. Pure templates."""
    eid = row["email_id"]
    sender = detail.get("from", "")
    subject = f"Re: {detail.get('subject', '')}"
    status = row.get("status")
    greeting = f"Dear {sender}," if sender else "Dear team,"

    if status == "MISMATCH":
        si, bl = _si_bl_docs(detail.get("docs", []))
        si_fields = (si or {}).get("fields", {}) or {}
        bl_fields = (bl or {}).get("fields", {}) or {}
        lines = []
        for f in row.get("defect_fields", []):
            label = FIELD_LABELS.get(f, f)
            lines.append(
                f"- {label}: SI shows \"{_fmt(si_fields.get(f))}\" vs "
                f"draft BL shows \"{_fmt(bl_fields.get(f))}\" "
                f"— please amend the BL to match the SI."
            )
        body = (
            f"{greeting}\n\n"
            f"Following verification of {eid} against the Shipping Instruction, "
            f"please amend the draft Bill of Lading as follows:\n\n"
            + "\n".join(lines)
            + "\n\nPlease confirm once amended and re-issue the draft BL.\n\n"
            "Thank you,\nOperations Team"
        )
        return {
            "kind": "amendment_request",
            "to": sender,  # operator corrects to carrier address if needed
            "subject": f"{subject} — BL amendment requested",
            "body": body,
        }

    reason = row.get("review_reason")
    docs = _doc_names(detail.get("docs", []))
    needs = {
        "missing_attachment": (
            "missing_document",
            "Draft BL requested",
            f"we could not find the draft Bill of Lading among the attachments "
            f"({docs}). Please send the draft BL for {eid} so we can complete "
            f"the comparison against the Shipping Instruction.",
        ),
        "wrong_doc_type": (
            "wrong_document",
            "Correct document requested",
            f"the attachment provided ({docs}) is not a draft Bill of Lading "
            f"(it appears to be a different document type). Please send the "
            f"actual draft BL for {eid}.",
        ),
        "unreadable": (
            "unreadable_document",
            "Document re-send requested",
            f"the attachment ({docs}) could not be opened or read "
            f"(corrupt, scanned image, or unsupported format). Please re-send "
            f"it as a searchable PDF, TXT, DOCX, or XLSX for {eid}.",
        ),
        "missing_value": (
            "incomplete_si",
            "Completed SI requested",
            "the Shipping Instruction contains placeholder values "
            "(e.g. ???, _______, TBA, N/A). Please send the completed SI with "
            f"all fields filled for {eid}.",
        ),
    }
    kind, doc_subject, ask = needs.get(
        reason,
        ("follow_up", "Follow-up requested",
         f"we need additional information to proceed with {eid}."),
    )
    body = (
        f"{greeting}\n\n"
        f"Regarding {eid}, {ask}\n\n"
        "Thank you,\nOperations Team"
    )
    return {
        "kind": kind,
        "to": sender,
        "subject": f"{subject} — {doc_subject}",
        "body": body,
    }


class ActionStore:
    """Append-only log of approved drafts. Supabase when configured."""

    def __init__(self, data_dir: str = "."):
        self.cfg = hitl.supabase_cfg()
        fname = "hitl_actions.json"
        p = Path(fname)
        self.local_path = p if p.is_absolute() else Path(data_dir) / p.name
        try:
            self._local = json.loads(self.local_path.read_text(encoding="utf-8"))
            if not isinstance(self._local, list):
                self._local = []
        except Exception:
            self._local = []

    @property
    def backend(self) -> str:
        return "supabase" if self.cfg["configured"] else "local"

    def save(self, record: dict) -> dict:
        record = dict(record)
        record["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record.setdefault("approved_by", "human")
        self._local.append(record)
        try:
            self.local_path.write_text(json.dumps(self._local, indent=2), encoding="utf-8")
        except Exception:
            pass
        if not self.cfg["configured"]:
            return {"backend": "local", "record": record}
        try:
            url = f"{self.cfg['url']}/rest/v1/hitl_actions"
            payload = json.dumps(record).encode()
            headers = {
                "apikey": self.cfg["key"],
                "Authorization": f"Bearer {self.cfg['key']}",
                "Content-Type": "application/json",
            }
            req = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
            return {"backend": "supabase", "record": record}
        except Exception as exc:
            return {"backend": "local (supabase failed)", "record": record,
                    "error": str(exc)}

    def for_email(self, email_id: str) -> list:
        if not self.cfg["configured"]:
            return [r for r in self._local if r.get("email_id") == email_id]
        try:
            url = (f"{self.cfg['url']}/rest/v1/hitl_actions"
                   f"?email_id=eq.{email_id}&select=*&order=created_at.desc")
            headers = {"apikey": self.cfg["key"],
                       "Authorization": f"Bearer {self.cfg['key']}"}
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return [r for r in self._local if r.get("email_id") == email_id]
