import json
import os
import re
from loader import Inbox


def normalize_text(val):
    if not val:
        return ""
    val = str(val).lower().strip()
    val = re.sub(r"[\s\t\n\r]+", " ", val)
    val = re.sub(r"[^\w\s]", "", val)
    return val


def extract_number(val):
    if not val:
        return None
    cleaned = str(val).replace(",", "")
    nums = re.findall(r"\d+(?:\.\d+)?", cleaned)
    if nums:
        try:
            # Extract index 0 to get the string element out of the matched list
            first_item = nums
            return float(first_item)
        except (ValueError, TypeError, IndexError):
            return None
    return None


def extract_fields_from_doc(text):
    """Extracts the 7 required fields from SI or BL text safely."""
    fields = {
        "shipper": None,
        "consignee": None,
        "notify_party": None,
        "port_of_loading": None,
        "port_of_discharge": None,
        "container_count": None,
        "gross_weight_kg": None,
    }

    if not text:
        return fields

    terminators = (
        r"(?=\s*(?:shipper|consignee|to the order of|notify|also notify|port of"
        r" loading|pol|load port|port of discharge|pod|discharge port|total"
        r" containers|container count|no\.?\s*of\s*containers|gross weight|gross"
        r" wt|gw|vessel|voyage|commodity|kinds of packages|hs code|booking"
        r" ref|oc no|freight|export carrier|bill of lading no|description|payment"
        r" terms|incoterms|net weight|invoice date|seller|buyer|\n|$))"
    )

    patterns = {
        "shipper": (
            r"(?:shipper\s*\\(principal\s*or\s*seller\\)|shipper/exporter|shipper|exporter)[\s:]*(.+?)"
            + terminators
        ),
        "consignee": (
            r"(?:consignee\s*\\(non-negotiable\\)|to the order of|consignee)[\s:]*(.+?)"
            + terminators
        ),
        "notify_party": r"(?:notify party|notify|also notify)[\s:]*(.+?)"
        + terminators,
        "port_of_loading": (
            r"(?:port of loading\s*\\(pol\\)|port of loading|load port|pol)[\s:]*(.+?)"
            + terminators
        ),
        "port_of_discharge": (
            r"(?:port of discharge\s*\\(pod\\)|port of discharge|discharge port|pod)[\s:]*(.+?)"
            + terminators
        ),
        "container_count": (
            r"(?:no\.?\s*of\s*containers\s*or\s*packages|total containers|container count|containers)[\s:]*(.+?)"
            + terminators
        ),
        "gross_weight_kg": (
            r"(?:gross weight毛重\s*\\(kgs\\)|gross wt\s*\\(kgs\\)|gross weight\s*\\(kg\\)|gross weight|gross wt|gw\s*\\(kg\\))[\s:]*(.+?)"
            + terminators
        ),
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match and match.group(1) is not None:
            raw_val = match.group(1).strip()
            if raw_val:
                if key in ["container_count", "gross_weight_kg"]:
                    fields[key] = extract_number(raw_val)
                else:
                    fields[key] = raw_val

    return fields


def classify_email(subject, body):
    text = f"{subject} {body}".lower()

    if any(
        k in text
        for k in [
            "unsubscribe",
            "winner",
            "click here",
            "casino",
            "crypto",
            "prize",
            "lottery",
        ]
    ):
        return "SPAM"
    if any(
        k in text
        for k in [
            "invoice",
            "billing",
            "payment",
            "receipt",
            "tax invoice",
            "statement of account",
            "overdue",
        ]
    ):
        return "INVOICE_QUERY"
    if any(
        k in text
        for k in [
            "new si",
            "prepare si",
            "shipping instruction request",
            "issue si",
            "submit si",
            "create si",
        ]
    ):
        return "SI_REQUEST"
    if any(
        k in text
        for k in [
            "compare",
            "bl draft",
            "draft bl",
            "verify bl",
            "check bl",
            "discrepancy",
            "si vs bl",
            "bill of lading",
            "review bl",
        ]
    ):
        return "BL_COMPARISON"

    return "GENERAL"


# =========================================================
# MAIN SCRIPT EXECUTION
# =========================================================
inbox = Inbox(".")
submission = {}

print(f"Loaded {len(inbox.emails())} emails. Starting processing...")

for email in inbox:
    eid = email["email_id"]
    subject = email.get("subject", "")
    body = email.get("body", "")
    attachments = email.get("attachments", [])

    # Stage 1: Categorization
    category = classify_email(subject, body)

    if category != "BL_COMPARISON":
        submission[eid] = {
            "category": category,
            "status": "OK",
            "review_reason": None,
            "has_defect": False,
            "defect_fields": [],
        }
        continue

    # Stage 2: Edge-case handling (missing attachments)
    if len(attachments) == 0 or (
        len(attachments) == 1 and "_SI" in str(attachments)
    ):
        if any(
            k in f"{subject} {body}".lower()
            for k in ["check", "compare", "verify", "attached"]
        ):
            submission[eid] = {
                "category": category,
                "status": "NEEDS_REVIEW",
                "review_reason": "missing_attachment",
                "has_defect": False,
                "defect_fields": [],
            }
            continue

    si_text, bl_text = None, None
    wrong_doc = False
    unreadable_doc = False

    for att_path in attachments:
        try:
            if hasattr(inbox, "read_text"):
                content = inbox.read_text(att_path)
            else:
                with open(att_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()

            if not content or len(content.strip()) == 0:
                unreadable_doc = True

            if any(
                doc_type in content.upper()
                for doc_type in [
                    "COMMERCIAL INVOICE",
                    "PACKING LIST",
                    "CERTIFICATE OF ORIGIN",
                ]
            ):
                wrong_doc = True

            if "_SI" in att_path:
                si_text = content
            elif "_BL" in att_path:
                bl_text = content
        except Exception:
            unreadable_doc = True

    if wrong_doc:
        submission[eid] = {
            "category": category,
            "status": "NEEDS_REVIEW",
            "review_reason": "wrong_doc_type",
            "has_defect": False,
            "defect_fields": [],
        }
        continue

    if unreadable_doc:
        submission[eid] = {
            "category": category,
            "status": "NEEDS_REVIEW",
            "review_reason": "unreadable",
            "has_defect": False,
            "defect_fields": [],
        }
        continue

    if si_text and any(
        placeholder in si_text
        for placeholder in ["???", "_______", "TBA", "TO BE ADVISED", "N/A"]
    ):
        submission[eid] = {
            "category": category,
            "status": "NEEDS_REVIEW",
            "review_reason": "missing_value",
            "has_defect": False,
            "defect_fields": [],
        }
        continue

    if not si_text or not bl_text:
        submission[eid] = {
            "category": category,
            "status": "OK",
            "review_reason": None,
            "has_defect": False,
            "defect_fields": [],
        }
        continue

    # Stage 3: Extraction & Discrepancy Comparison
    si_fields = extract_fields_from_doc(si_text)
    bl_fields = extract_fields_from_doc(bl_text)

    mismatched_fields = []
    target_fields = [
        "shipper",
        "consignee",
        "notify_party",
        "port_of_loading",
        "port_of_discharge",
        "container_count",
        "gross_weight_kg",
    ]

    for field in target_fields:
        v_si = si_fields.get(field)
        v_bl = bl_fields.get(field)

        if v_si is not None and v_bl is not None:
            if field in ["container_count", "gross_weight_kg"]:
                if v_si != v_bl:
                    mismatched_fields.append(field)
            else:
                if normalize_text(v_si) != normalize_text(v_bl):
                    mismatched_fields.append(field)

    mismatched_fields.sort()
    has_defect = len(mismatched_fields) > 0
    status = "MISMATCH" if has_defect else "OK"

    submission[eid] = {
        "category": category,
        "status": status,
        "review_reason": None,
        "has_defect": has_defect,
        "defect_fields": mismatched_fields,
    }

# Save results
with open("submission.json", "w") as f:
    json.dump(submission, f, indent=2)

print("Successfully processed all 520 records!")

status_counts = {}
for v in submission.values():
    st = v["status"]
    status_counts[st] = status_counts.get(st, 0) + 1
print("Status Breakdown:", status_counts)
