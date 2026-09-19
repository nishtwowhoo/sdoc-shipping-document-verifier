"""Extract the 7 comparison fields from a parsed SI or BL document text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from formats import looks_like_container_row

# field name -> list of label synonyms observed in the generator (pools.LABELS)
# and in rendered SI/BL documents (SI and BL differ in the label vocabulary).
FIELDS: list[str] = [
    "shipper",
    "consignee",
    "notify_party",
    "port_of_loading",
    "port_of_discharge",
    "container_count",
    "gross_weight_kg",
]

_LABEL_ALIASES: dict[str, list[str]] = {
    "shipper": [
        "Shipper/Exporter",
        "Shipper (Principal or Seller)",
        "Shipper",
    ],
    "consignee": [
        "Consignee (Non-Negotiable)",
        "Consignee",
        "To The Order Of",
    ],
    "notify_party": [
        "Notify Party/Intermediate Consignee",
        "Notify Party",
        "Notify",
    ],
    "port_of_loading": [
        "Port of Loading (POL)",
        "Port of Loading",
        "Load Port",
        "POL",
    ],
    "port_of_discharge": [
        "Port of Discharge (POD)",
        "Port of Discharge",
        "Discharge Port",
        "POD",
    ],
    "container_count": [
        "No. of Containers or Packages",
        "No. of Containers",
        "Total Containers",
        "Container Count",
    ],
    "gross_weight_kg": [
        "Total Gross Weight (KG)",
        "Gross Weight (KG)",
        "Gross Wt (Kgs)",
        "Gross Weight毛重(KGS)",
        "Gross Weight",
        "GROSS WEIGHT",
    ],
}

_PLACEHOLDER_PATTERNS = [
    re.compile(r"^\W+$"),               # ___, ???, ---
    re.compile(r"^[-\s]*$"),            # blank
    re.compile(r"^(na|n\/?a|tba|tbd|tbc|xxx|n\/?c)\b", re.I),  # N/A, TBA ...
    re.compile(r"_{3,}"),               # ____MT, ________
]

_port_code_re = re.compile(r"\s*\([^)]*\)\s*$")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize(value: str) -> str:
    """Canonical lower-cased form used for equality comparison."""
    value = _port_code_re.sub("", value)   # strip trailing "(CODE)"
    value = value.lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def is_placeholder(value: str) -> bool:
    if value is None:
        return True
    value = value.strip()
    if not value:
        return True
    for pat in _PLACEHOLDER_PATTERNS:
        if pat.search(value):
            return True
    return False


def _strip_label_artifact(rest: str) -> str:
    """Drop the "(发货人)"-style label leftovers glued onto docx/xlsx values."""
    rest = rest.strip()
    while rest:
        if rest.startswith("("):
            end = rest.find(")")
            if end == -1:
                break
            rest = rest[end + 1:].lstrip(":： \t-=")
            continue
        if re.match(r"^[^\x00-\x7f]+[:：]?", rest):
            rest = re.sub(r"^[^\x00-\x7f]+[:：]?\s*", "", rest)
            continue
        break
    return rest.strip()


def _first_line(value: str) -> str:
    line = re.split(r"[\n|]+", value, maxsplit=1)[0]
    return line.strip().rstrip(",")


def _int(value: str) -> int | None:
    m = re.search(r"\d+", value)
    return int(m.group()) if m else None


def _semantic(field: str, value: str) -> str:
    if field in ("shipper", "consignee", "notify_party"):
        return normalize(value)
    if field in ("port_of_loading", "port_of_discharge"):
        return normalize(_first_line(value))
    if field == "container_count":
        n = _int(value.split("x")[0]) if "x" in value.lower() else _int(value)
        return str(n) if n is not None else normalize(value)
    if field == "gross_weight_kg":
        m = re.search(r"([\d,]+)\s*(?:kgs?|kg|mt|kilos)?", value, re.I)
        if m:
            return str(int(m.group(1).replace(",", "")))
        return normalize(value)
    return normalize(value)


def _aliases(field: str) -> list[str]:
    out: list[str] = []
    for a in _LABEL_ALIASES[field]:
        a = re.sub(r"\s+", " ", a.strip().lower())
        if a and a not in out:
            out.append(a)
    return out


_SORTED = sorted(
    [(a, f) for f in FIELDS for a in _aliases(f)],
    key=lambda pair: len(pair[0]),
    reverse=True,
)


def _match_label(line: str) -> tuple[str, str] | None:
    """Return (field, rest) if line starts with a known label."""
    low = re.sub(r"\s+", " ", line.strip()).lower()
    for alias, field in _SORTED:
        if low.startswith(alias):
            rest = line[len(alias):]
            rest = rest.lstrip(":.=- \t")
            return field, _strip_label_artifact(rest)
    return None


@dataclass
class _Candidate:
    field: str
    value: str
    total: bool = False


def _collect(text: str) -> dict[str, str]:
    """Map field -> raw value using first label occurrence (TOTAL wins for GW)."""
    lines = text.splitlines()
    candidates: dict[str, list[_Candidate]] = {f: [] for f in FIELDS}
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        field = None
        value = ""
        total = False
        m = _match_label(line)
        if m is None and line.lower().startswith("total "):
            # PDFs render the gross-weight line as "TOTAL <label>: ..."
            m = _match_label(line[len("total "):])
            if m:
                field, value = m
                total = True
        elif m is not None:
            field, value = m
        if field is None:
            i += 1
            continue
        if not value:
            # value lives on the following lines until the next label/table
            j = i + 1
            block: list[str] = []
            while j < n:
                nxt = lines[j].strip()
                if not nxt or _match_label(nxt) or looks_like_container_row(nxt):
                    break
                block.append(nxt)
                j += 1
            value = " | ".join(block)
        i += 1
        if value:
            candidates[field].append(_Candidate(field, value, total))
    out: dict[str, str] = {}
    for f in FIELDS:
        list_ = candidates[f]
        if not list_:
            continue
        totals = [c for c in list_ if c.total]
        chosen = totals[0] if totals else list_[0]
        out[f] = chosen.value
    return out


def build_document_fields(text: str) -> dict[str, str]:
    """Return field -> semantic value ("" when label missing or placeholder)."""
    raw = _collect(text)
    out: dict[str, str] = {}
    for f in FIELDS:
        v = raw.get(f, "")
        if is_placeholder(v):
            out[f] = ""
        else:
            out[f] = _semantic(f, v)
    return out


@dataclass
class Doc:
    path: str
    readable: bool
    kind: str  # shipping_instruction | bill_of_lading | other
    fields: dict[str, str] = field(default_factory=dict)
    text: str = ""