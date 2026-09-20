"""Attachment reading: turn any SI/BL attachment into plain text lines.

Supported: .txt, .pdf (text layer), .docx, .xlsx. Returns (text, readable).
When a file is empty, image-only, corrupt or otherwise has no text layer we
return readable=False so the comparator can escalate to `unreadable`.
"""

from __future__ import annotations

import re
import typing  # noqa: F401

CONTAINER_RE = re.compile(r"^[A-Z]{4}\d{6,7}\b")


def read_text(path: str, raw: bytes) -> tuple[str, bool]:
    """Return (text, readable)."""
    low = path.lower()
    if len(raw) == 0:
        return "", False
    try:
        if low.endswith(".txt"):
            return raw.decode("utf-8", errors="replace"), True
        if low.endswith((".pdf", ".pdfa")):
            return _read_pdf(raw)
        if low.endswith(".docx"):
            return _read_docx(raw)
        if low.endswith(".xlsx"):
            return _read_xlsx(raw)
        # unknown binary -> try utf-8, else unreadable
        return raw.decode("utf-8", errors="replace"), True
    except Exception:  # noqa: BLE001 - any parse failure => unreadable
        return "", False


def _read_pdf(raw: bytes) -> tuple[str, bool]:
    from pypdf import PdfReader
    from io import BytesIO

    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(BytesIO(raw)).pages
    )
    if not text.strip():
        return "", False
    return text, True


def _read_docx(raw: bytes) -> tuple[str, bool]:
    import docx
    from io import BytesIO

    d = docx.Document(BytesIO(raw))
    lines: list[str] = []
    for p in d.paragraphs:
        if p.text.strip():
            lines.append(p.text)
    for t in d.tables:
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells]
            if cells[0] and cells[1]:
                lines.append(f"{cells[0]}: {' '.join(cells[1].split())}")
            elif cells[0]:
                lines.append(cells[0])
    return "\n".join(lines), True


def _read_xlsx(raw: bytes) -> tuple[str, bool]:
    import openpyxl
    from io import BytesIO

    wb = openpyxl.load_workbook(BytesIO(raw), data_only=True)
    lines: list[str] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            label = str(row[0]).strip() if row and row[0] is not None else ""
            value = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
            if label or value:
                # xlsx joins party name and address with " | " / "; "
                value = value.replace("|", " | ").replace(";", " | ")
                lines.append(f"{label}: {value}" if label else value)
    return "\n".join(lines), True


def looks_like_container_row(line: str) -> bool:
    """True for a PDF container-table row (junk when scanning labels)."""
    return bool(CONTAINER_RE.match(line.strip()))