"""Rule-based classifier tuned to the generation templates.

Priority: 1) spam signals, 2) SI/BL attachments => BL_COMPARISON,
3) SI_REQUEST / INVOICE_QUERY / BL_COMPARISON subject+body templates,
4) fallback GENERAL. Needs-revision edges are all BL_COMPARISON and carry
attachments, so rule 2 keeps them routed correctly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CATEGORIES = ("BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM")

_SPAM_DOMAINS = (
    "crypto-invest.net",
    "secure-mailbox.org",
    "webmail-verify.co",
    "prize-claims.info",
    "logistics-deals.biz",
    "parcel-track.co",
)

_SPAM_BODY = (
    r"verify your account",
    r"storage limit",
    r"gift card",
    r"one weird trick",
    r"limited time offer",
    r"90% off",
    r"claim your",
    r"bitcoin",
    r"crypto",
    r"monthly draw",
    r"customs fee",
    r"unpaid",
    r"investment opportunity",
    r"track-\w+\.co",
    r"reduce your shipping (cost|expense)",
    r"renew your (subscription|account)|suspended",
)

_SI_RE = re.compile(
    r"request si|si needed|^re[:_ -]*\s*si\b|\bsi -|cust si|\bshipping instruction",
    re.I,
)
_AUTOMATION_RE = re.compile(
    r"_rpa_|india hss sd billing|completed successfully|rpa bot|no action required",
    re.I,
)
_INVOICE_RE = re.compile(
    r"query on invoice|cancel invoice|invoice\s*\d+|local charge|\bthc\b|"
    r"d\s*&\s*d|detention|gr is still missing|total freight|rak billing|missing gr",
    re.I,
)
_BL_RE = re.compile(
    r"draft bl|bl draft|amend bl|confirm docs|to confirm docs|"
    r"bl no\.|the draft bill of lading|compare the (si|shipping instruction)",
    re.I,
)

_spam_body_re = re.compile("|".join(_SPAM_BODY), re.I)


@dataclass
class Classification:
    category: str
    confidence: float = 0.1
    reasons: list[str] | None = None


def _spam_from(addr: str) -> bool:
    low = (addr or "").lower()
    return "@" in low and any(d in low for d in _SPAM_DOMAINS)


def classify(email: dict) -> Classification:
    subject = email.get("subject", "") or ""
    body = email.get("body", "") or ""
    sender = email.get("from", "") or ""
    hay = f"{subject}\n{body}"

    if _spam_from(sender) or _spam_body_re.search(hay):
        return Classification("SPAM", 0.99, ["spam signals"])

    attachments = email.get("attachments") or []
    if attachments:
        joined = " ".join(attachments).upper()
        if "_SI" in joined or "_BL" in joined or "ATTACHMENTS" not in email.get("attachments", ""):
            return Classification("BL_COMPARISON", 0.95, ["has SI/BL attachment"])

    if _SI_RE.search(hay):
        return Classification("SI_REQUEST", 0.92, ["shipping-instruction request"])

    if not _AUTOMATION_RE.search(hay) and _INVOICE_RE.search(hay):
        return Classification("INVOICE_QUERY", 0.9, ["invoice/billing query"])

    if _BL_RE.search(hay):
        return Classification("BL_COMPARISON", 0.85, ["draft BL template"])

    return Classification("GENERAL", 0.5)