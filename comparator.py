"""Compare SI vs draft BL attachments for a single email -> per-email result."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from formats import read_text
from extractor import (
    FIELDS,
    build_document_fields,
    Doc,
    is_placeholder,
)

REVIEW_REASONS = ("wrong_doc_type", "missing_attachment", "unreadable", "missing_value")

# order matters: "bill of lading" is a substring of the SI title too
_OTHER_MARKERS = (
    ("commercial invoice", "invoice no"),
    ("packing list",),
    ("certificate of origin",),
)
_SI_MARKERS = ("bill of lading instruction", "shipping instruction", "bl instruction", "b/l instruction")
_BL_MARKERS = ("bill of lading (draft)", "bill of lading", "draft bill of lading")


def detect_kind(path: str, text: str, readable: bool) -> str:
    if not readable or not text:
        return "other"
    low = text.lower()
    if any(m in low for m in ("commercial invoice", "packing list", "certificate of origin")):
        return "other"
    if any(m in low for m in _SI_MARKERS):
        return "shipping_instruction"
    if any(m in low for m in _BL_MARKERS):
        return "bill_of_lading"
    up = path.upper()
    if "_SI" in up:
        return "shipping_instruction"
    if "_BL" in up:
        return "bill_of_lading"
    return "other"


def _parse_doc(inbox, path: str) -> Doc:
    raw = inbox.read_bytes(path)
    text, readable = read_text(path, raw)
    kind = detect_kind(path, text, readable)
    fields = build_document_fields(text) if readable else {}
    return Doc(path=path, readable=readable, kind=kind, fields=fields, text=text)


def _missing_fields(doc: Doc) -> list[str]:
    if not doc.readable or doc.kind != "shipping_instruction":
        return []
    return [f for f in FIELDS if is_placeholder(doc.fields.get(f, "") or "")]


@dataclass
class Result:
    email_id: str
    category: str
    status: str = "OK"
    review_reason: str | None = None
    defect_fields: list[str] = field(default_factory=list)

    @property
    def has_defect(self) -> bool:
        return self.status == "MISMATCH"

    def as_dict(self) -> dict:
        out = {
            "category": self.category,
            "status": self.status,
            "review_reason": self.review_reason,
            "defect_fields": self.defect_fields,
            "has_defect": self.has_defect,
        }
        return out


def analyze_email(inbox, email, category: str) -> Result:
    eid = email["email_id"]
    result = Result(email_id=eid, category=category)
    paths = email.get("attachments") or []

    if category != "BL_COMPARISON":
        return result

    if not paths:
        # plain "please send the draft BL" requests are OK; dropped-attachment
        # comparison requests (edge cases) are NEEDS_REVIEW / missing_attachment
        body = email.get("body", "") or ""
        if re.search(
            r"compare the si|appear to have been dropped|is still missing",
            body,
            re.I,
        ):
            return Result(
                email_id=eid,
                category=category,
                status="NEEDS_REVIEW",
                review_reason="missing_attachment",
            )
        return result

    if len(paths) == 1:
        return Result(
            email_id=eid,
            category=category,
            status="NEEDS_REVIEW",
            review_reason="missing_attachment",
        )

    docs = [_parse_doc(inbox, p) for p in paths]

    if any(not d.readable for d in docs):
        return Result(
            email_id=eid, category=category, status="NEEDS_REVIEW", review_reason="unreadable"
        )

    kinds = [d.kind for d in docs]
    if any(k == "other" for k in kinds):
        return Result(
            email_id=eid, category=category, status="NEEDS_REVIEW", review_reason="wrong_doc_type"
        )

    si = next((d for d in docs if d.kind == "shipping_instruction"), None)
    bl = next((d for d in docs if d.kind == "bill_of_lading"), None)

    if si is None or bl is None:
        # two readable docs but none matching an SI/BL slot (e.g. two SI copies)
        return Result(
            email_id=eid, category=category, status="NEEDS_REVIEW", review_reason="wrong_doc_type"
        )

    missing = _missing_fields(si)
    if missing and is_placeholder(next((si.fields.get(f, "") for f in missing), "")):
        return Result(
            email_id=eid, category=category, status="NEEDS_REVIEW", review_reason="missing_value"
        )

    defects = [
        f
        for f in FIELDS
        if (si.fields.get(f) or "") != (bl.fields.get(f) or "")
        and not is_placeholder(si.fields.get(f) or "")
        and not is_placeholder(bl.fields.get(f) or "")
    ]
    if defects:
        return Result(email_id=eid, category=category, status="MISMATCH", defect_fields=sorted(defects))
    return Result(email_id=eid, category=category, status="OK")