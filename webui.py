"""Minimal dependency-free web UI for the SDOC pipeline.

SaaS-style dashboard (three-column shell, KPI strip, filter row, metrics
table) reusing the existing pipeline (classifier/extractor/comparator).
Run from the shipment_scan folder:

    python webui.py [--data-dir data] [--gt data/ground_truth.json] [--port 8081]

Open http://127.0.0.1:8081  (default port avoids the docker server on 8080).
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import io
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from classifier import CATEGORIES, classify
from comparator import REVIEW_REASONS, analyze_email, _parse_doc
from extractor import FIELDS
from loader import Inbox

STATUSES = ("OK", "MISMATCH", "NEEDS_REVIEW")

# ---------------------------------------------------------------------------
# data layer
# ---------------------------------------------------------------------------


def _build(data_dir: str):
    inbox = Inbox(data_dir)
    rows, details = [], {}
    for email in inbox.emails():
        cls = classify(email)
        result = analyze_email(inbox, email, cls.category)
        row = result.as_dict()
        row.update(
            {
                "email_id": email["email_id"],
                "from": email.get("from", ""),
                "subject": email.get("subject", ""),
                "confidence": cls.confidence,
            }
        )
        rows.append(row)
        docs = []
        for path in email.get("attachments") or []:
            try:
                doc = _parse_doc(inbox, path)
                docs.append(
                    {
                        "path": doc.path,
                        "kind": doc.kind,
                        "readable": doc.readable,
                        "fields": doc.fields,
                        "text": doc.text[:4000],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                docs.append(
                    {"path": path, "kind": "error", "readable": False,
                     "fields": {}, "text": f"<parse error: {exc}>"}
                )
        details[email["email_id"]] = {"docs": docs, "body": email.get("body", "")}
    rows.sort(key=lambda r: r["email_id"])
    return inbox, rows, details


def _score_report(sub: dict, gt_path: str):
    if not Path(gt_path).is_file():
        return None
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scoring

    truth = json.loads(Path(gt_path).read_text(encoding="utf-8"))
    return json.loads(json.dumps(scoring.score_all(truth, sub), default=str))


def _ui_metrics(r: dict, extra: dict) -> dict:
    """Per-row dashboard widgets (pills, channels, metric bars, headline)."""
    cat, st = r["category"], r["status"]
    docs = extra["docs"]
    conf = r.get("confidence", 0.9)

    pill_tones = {
        "BL_COMPARISON": "teal",
        "SI_REQUEST": "green",
        "INVOICE_QUERY": "amber",
        "GENERAL": "grey",
        "SPAM": "rose",
    }
    pills = [(cat.lower(), pill_tones[cat])]

    if cat == "BL_COMPARISON":
        n_docs = len(docs)
        parsed = sum(1 for d in docs if d["readable"])
        si = next((d for d in docs if d["kind"] == "shipping_instruction"), None)
        kinds = {d["kind"] for d in docs}
        if si and "bill_of_lading" in kinds:
            pills.append(("si+bl", "teal"))
        elif any(k not in ("shipping_instruction", "bill_of_lading") for k in kinds):
            pills.append(("doc", "grey"))
        if st == "NEEDS_REVIEW":
            pills.append((r["review_reason"], "amber"))

        metrics = []
        if n_docs == 0:
            metrics.append(("Attachment", "missing", 0, "amber"))
        else:
            pct = 100 if parsed == n_docs else int(parsed / n_docs * 100)
            metrics.append(("Parsed", f"{parsed}/{n_docs}", pct, "green" if pct == 100 else "amber"))
            if parsed == n_docs and si:
                nf = sum(1 for v in si["fields"].values() if v)
                metrics.append(("Extracted", f"{nf}/7", int(nf / 7 * 100), "teal"))
        if st == "MISMATCH":
            n = len(r["defect_fields"])
            metrics.append(("Defects", str(n), min(100, n * 15), "amber" if n else "green"))
        metrics = metrics[:3]

        if st == "OK":
            fid = ("100%", "Fields match", "teal")
        elif st == "MISMATCH":
            fid = ("100%", f"{len(r['defect_fields'])} defect(s) flagged", "amber")
        else:
            fid = ("100%", (r["review_reason"] or "escalated").replace("_", " "), "amber")
        return {"pills": pills, "channels": n_docs, "metrics": metrics, "fid": fid}

    pct = round(conf * 100)
    col = "teal" if pct >= 80 else "amber"
    metrics = [("Confidence", f"{pct}%", pct, col), ("Rules", "1/1", 100, "green")]
    fid = {
        "SI_REQUEST": (f"{pct}%", "Routing confidence", "teal"),
        "INVOICE_QUERY": (f"{pct}%", "Matching", "teal"),
        "GENERAL": ("50%", "Fallback", "grey"),
        "SPAM": ("100%", "Blocked", "green"),
    }[cat]
    return {"pills": pills, "channels": 1, "metrics": metrics, "fid": fid}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _jsesc(s) -> str:
    return _esc(json.dumps(str(s) if s is not None else "", ensure_ascii=False))


_ICONS = {
    "dashboard": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    "inbox": '<path d="M3 13h4l2 3h6l2-3h4"/><path d="M3 13V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v8"/><path d="M3 13v4a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-4"/>',
    "doc": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>',
    "clip": '<path d="M21.5 12.5 12 22a6 6 0 0 1-8.5-8.5l9.5-9.5a4 4 0 0 1 5.7 5.7L9 19.2a2 2 0 0 1-2.8-2.8l7.3-7.3"/>',
    "flag": '<path d="M4 22V4"/><path d="M4 4h12l-2 4 2 4H4"/>',
    "bell": '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
    "shield": '<path d="M12 3l7 3v5c0 5-3.5 8-7 9-3.5-1-7-4-7-9V6z"/><path d="M9.5 12l2 2 3.5-4"/>',
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.6-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3h.1a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9v.1a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5A2.5 2.5 0 1 1 12 13c-.8.6-1 1.6-1 2"/><circle cx="12" cy="18" r=".5"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-6 8-6s8 2 8 6"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4M16 3v4M3 11h18"/>',
    "sliders": '<path d="M4 6h9M17 6h3M6 12h11M19 12h1M4 18h7M15 18h5"/><circle cx="15" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="13" cy="18" r="2"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/>',
    "export": '<path d="M12 3v12"/><path d="M8 7l4-4 4 4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    "more": '<circle cx="12" cy="5" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="12" cy="19" r="1.4"/>',
    "check": '<path d="M20 6L9 17l-5-5"/>',
}


def _svg(name: str, size: int, cls: str = "",
         stroke: str = "currentColor", fill: str = "none", sw: float = 1.8) -> str:
    inner = _ICONS[name]
    if name == "more" or name == "user":
        fill = "currentColor"
    style = f" class=\"{cls}\"" if cls else ""
    return (f'<svg viewBox="0 0 24 24" width="{size}" height="{size}"{style} '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" '
            'stroke-linecap="round" stroke-linejoin="round">' + inner + "</svg>")


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------

_CSS = """
:root{--rail:#0B2035;--side:#0F2B46;--teal:#00A5A5;--green:#3BB273;--amber:#F5A623;
--bg:#F5F6F8;--surface:#fff;--text:#222B36;--muted:#6B7280;--border:#E4E7EB}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;font:13.5px/1.5 system-ui,-apple-system,"Segoe UI",Inter,Roboto,sans-serif;
color:var(--text);background:var(--bg);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
html{scroll-behavior:smooth}
:focus-visible{outline:2px solid rgba(0,165,165,.45);outline-offset:2px;border-radius:4px}
a{color:var(--teal);text-decoration:none}
h1,h2{font-weight:600}
.app{display:flex;min-height:100vh}
/* --- icon rail --- */
.rail{width:56px;flex:none;background:var(--rail);display:flex;flex-direction:column;
align-items:center;padding:16px 0;gap:6px;position:sticky;top:0;height:100vh;align-self:flex-start}
.rail .ico{width:38px;height:38px;display:flex;align-items:center;justify-content:center;
border-radius:8px;color:#5B7089;cursor:pointer;border:0;background:none}
.rail .ico:hover{color:#9FB3C6}
.rail .ico.active{color:var(--teal);background:rgba(0,165,165,.10)}
.rail .pin{margin-top:auto}
/* --- sidebar --- */
.side{width:210px;flex:none;background:var(--side);color:#8FA6BC;padding:18px 14px;
display:flex;flex-direction:column;gap:4px;overflow-y:auto;position:sticky;top:0;height:100vh;align-self:flex-start}
.wordmark{font-size:15px;font-weight:600;color:#fff;letter-spacing:.3px;
padding:2px 6px 16px}
.wordmark .dot{color:var(--teal)}
.side .ssearch{position:relative;margin-bottom:14px}
.side .ssearch input{width:100%;height:34px;border:0;border-radius:17px;outline:0;
padding:0 12px 0 32px;font-size:12px;color:var(--text);background:#fff}
.side .ssearch .sr-ico{position:absolute;left:10px;top:50%;transform:translateY(-50%);
color:#9AA6B2;display:flex}
.side .shead{font-size:10px;letter-spacing:.8px;text-transform:uppercase;
color:#5B7089;margin:16px 6px 6px}
.side .sitem{font-size:12.5px;color:#9FB3C6;padding:5px 8px;border-radius:6px;cursor:pointer;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border:0;background:none;text-align:left;width:100%}
.side .sitem:hover{color:#fff;background:rgba(255,255,255,.05)}
.side .sitem.active{color:#fff;background:rgba(0,165,165,.14)}
/* --- main --- */
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{display:flex;align-items:center;gap:16px;padding:16px 28px 12px;flex-wrap:wrap}
.breadcrumb{font-size:12px;color:var(--muted)}
.breadcrumb b{color:var(--text);font-weight:500}
.title{font-size:20px;font-weight:600;margin:2px 0 0;display:flex;align-items:center;gap:10px}
.title select{font-size:16px;font-weight:600;color:var(--teal);border:0;outline:0;background:none;cursor:pointer}
.topbar .sp{flex:1}
.btn{height:34px;padding:0 14px;border-radius:6px;font-size:13px;font-weight:500;
cursor:pointer;display:inline-flex;align-items:center;gap:7px;background:var(--surface);
transition:background .15s,border-color .15s,color .15s}
.btn.ghost{color:var(--text);border:1px solid var(--border)}
.btn.ghost:hover{border-color:#c9cfd6}
.btn.outline{color:var(--teal);border:1px solid var(--teal)}
.btn.outline:hover{background:rgba(0,165,165,.06)}
/* --- card + kpi strip --- */
.card{background:var(--surface);border:1px solid var(--border);border-radius:6px;
box-shadow:0 1px 2px rgba(15,23,42,.03);margin:0 28px 28px}
.tabs{display:flex;border-bottom:1px solid var(--border)}
.tab{flex:none;min-width:132px;padding:18px 22px 16px;cursor:pointer;border:0;background:none;
text-align:left;border-radius:0}
.tab .n{font-size:28px;font-weight:600;line-height:1.1;color:var(--text);font-variant-numeric:tabular-nums}
.tab .l{font-size:10.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--muted);margin-top:3px}
.tab.active{background:var(--teal)}
.tab.active .n,.tab.active .l{color:#fff}
.tab.amber .n{color:var(--amber)}
.tab.active.amber{background:var(--amber)}
.tab.active.amber .n,.tab.active.amber .l{color:#fff}
/* --- filter row --- */
.filters{display:flex;align-items:center;gap:10px;padding:16px 28px 12px;flex-wrap:wrap}
.field{height:36px;border:1px solid var(--border);border-radius:4px;background:var(--surface);
display:inline-flex;align-items:center;gap:8px;padding:0 12px;color:var(--muted);font-size:13px}
.field.fsearch{width:260px;position:relative;padding-left:34px}
.field.fsearch input{width:100%;border:0;outline:0;color:var(--text);font-size:13px;background:none}
.field .fi{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:#9AA6B2;display:flex}
.field input,select.field{outline:0;color:var(--text);cursor:pointer}
select.field{appearance:none;-webkit-appearance:none;padding-right:26px;
background-image:url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6" viewBox="0 0 10 6"><path d="M1 1l4 4 4-4" stroke="%236B7280" fill="none" stroke-width="1.5"/></svg>');
background-repeat:no-repeat;background-position:right 10px center}
.filters .sp{flex:1}
.morefilters{font-size:12.5px;color:var(--muted);display:inline-flex;align-items:center;gap:6px;cursor:pointer;border:0;background:none}
.morefilters:hover{color:var(--text)}
.help{padding:0 28px 14px;font-size:12px;color:var(--muted)}
.help b{color:var(--text);font-weight:500}
/* --- table --- */
table{width:100%;border-collapse:collapse}
thead th{font-size:11px;letter-spacing:.4px;text-transform:uppercase;color:var(--muted);
font-weight:600;text-align:left;padding:12px 28px 12px 0;border-bottom:1px solid var(--border);white-space:nowrap;
position:sticky;top:0;background:var(--surface);z-index:1}
thead th.th-email{padding-left:28px;text-align:center}
thead th.th-end{text-align:right}
tbody td{padding:18px 28px 18px 0;border-bottom:1px solid var(--border);vertical-align:middle;transition:background .12s}
tbody tr:hover td{background:#F8FAFB}
td.cel-name{text-align:center;padding-left:28px;padding-right:28px}
.cel-name .chan,.cel-name .pills{justify-content:center}
tbody tr:last-child td{border-bottom:0}
tbody tr:first-child td{border-top:0}
tbody tr.name{padding-left:28px}
.cname{font-size:13.5px;font-weight:600;color:var(--teal);display:inline-flex;align-items:center;gap:5px;transition:color .12s}
.cname:hover{color:#007f7f}
.chan{display:flex;gap:5px;margin-top:6px;color:#9AA6B2}
.pills{display:flex;gap:5px;margin-top:7px;flex-wrap:wrap}
.pill{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;border-radius:10px;padding:2px 8px}
.pill.teal{background:rgba(0,165,165,.12);color:#067a7a}
.pill.green{background:rgba(59,178,115,.14);color:#1f7a4d}
.pill.amber{background:rgba(245,166,35,.16);color:#b97706}
.pill.grey{background:#EEF1F4;color:var(--muted)}
.pill.rose{background:rgba(176,42,55,.10);color:#b02a37}
.cell-txt{font-size:13px;color:var(--text)}
.cell-mut{font-size:12.5px;color:var(--muted)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;vertical-align:1px}
.dot.green{background:var(--green)}.dot.red{background:#e0483e}.dot.amber{background:var(--amber)}.dot.grey{background:#B6C0CC}
.threed{width:30px;height:30px;border:0;border-radius:6px;background:none;color:var(--muted);cursor:pointer;display:flex;align-items:center;justify-content:center}
.threed:hover{background:#F0F2F5;color:var(--text)}
/* metrics + headline */
.met{margin-bottom:10px}
.met:last-child{margin-bottom:0}
.met .mt{display:flex;justify-content:space-between;gap:12px;font-size:12px}
.met .mt span{color:var(--muted)}
.met .mt b{font-weight:600;font-variant-numeric:tabular-nums}
.bar{height:4px;border-radius:2px;background:#EEF1F4;margin-top:4px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:2px}
.fid{font-size:17px;font-weight:600;line-height:1.2;font-variant-numeric:tabular-nums}
.fid .cap{font-size:11.5px;color:var(--muted);font-weight:400;margin-top:2px;text-transform:capitalize}
/* detail page */
.dtitle{font-size:18px;font-weight:600;margin:0 0 4px}
.subrow{display:flex;gap:22px;align-items:center;flex-wrap:wrap;margin:10px 0 16px}
.metgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}
.fmt{white-space:pre-wrap;font:12px/1.5 ui-monospace,Consolas,monospace;background:#F7F9FB;border:1px solid var(--border);border-radius:6px;padding:12px;max-height:320px;overflow:auto}
.keyval{font-size:12.5px;color:var(--muted)}
.keyval b{color:var(--text);font-weight:500}
details summary{cursor:pointer}
section{margin-bottom:0}
h3{font-size:13px;font-weight:600;margin:0 0 8px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
.chip{display:inline-block;background:#EEF1F4;border-radius:999px;padding:2px 9px;font-size:11px;margin:2px}
@media (max-width:1200px){.side{display:none}.tabs{overflow-x:auto}.tab{min-width:110px}}
"""

_RAIL = (
    '<button class="ico active" title="Overview">{dc}</button>'
    '<button class="ico" title="Inbox" data-tab=""><span style="color:inherit">{ib}</span></button>'
    '<button class="ico" title="BL comparisons" data-cat="BL_COMPARISON">{dc}</button>'
    '<button class="ico" title="SI requests" data-cat="SI_REQUEST">{dp}</button>'
    '<button class="ico" title="Invoice queries" data-cat="INVOICE_QUERY">{fl}</button>'
    '<button class="ico" title="Spam" data-cat="SPAM">{bl}</button>'
    '<button class="ico" title="Reviews" data-status="NEEDS_REVIEW">{sg}</button>'
    '<div class="pin"></div>'
    '<button class="ico" title="Usage">{gp}</button>'
    '<button class="ico" title="Account">{ur}</button>'
).format(
    dc=_svg("dashboard", 18), ib=_svg("inbox", 18), dp=_svg("doc", 18),
    fl=_svg("flag", 18), bl=_svg("bell", 18), sg=_svg("shield", 18),
    gp=_svg("gear", 18), ur=_svg("user", 18),
)

_SIDEBAR_LINKS = [
    ("Product", "Inbox overview", "all", "all"),
    ("Product", "BL comparisons", "cat", "BL_COMPARISON"),
    ("Product", "SI requests", "cat", "SI_REQUEST"),
    ("Product", "Invoice queries", "cat", "INVOICE_QUERY"),
    ("Product", "Spam review", "cat", "SPAM"),
    ("Product", "Edge cases", "status", "NEEDS_REVIEW"),
]

_SIDEBAR = """
<div class="wordmark">Shipment <span class="dot">·</span> Scan</div>
<div class="ssearch"><span class="sr-ico">{search}</span>
  <input id="side-search" placeholder="Search dashboard…"></div>
<div class="shead">Product dashboards</div>
{links}
<div class="shead">Custom dashboards</div>
<div class="sitem" data-score="1">Ground-truth scorecard</div>
<div class="sitem">Export log</div>
""".format(
    search=_svg("search", 14),
    links="".join(
        '<div class="sitem{act}"{attr}>{label}</div>'.format(
            act=" active" if label == "Inbox overview" else "",
            attr=(" data-all=1" if kind == "all"
                  else f' data-{kind}={json.dumps(value)}'),
            label=label,
        )
        for _, label, kind, value in _SIDEBAR_LINKS
    ),
)

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@TITLE@ · SDOC</title>
<style>@CSS@</style></head><body>
<div class="app">
  <nav class="rail">@RAIL@</nav>
  <nav class="side">@SIDEBAR@</nav>
  <main class="main">
    <div class="topbar">
      <div>
        <div class="breadcrumb">Shipment Scan <b>›</b> All emails</div>
        <div class="title">Inbox for
          <select><option>bundle</option><option>local data</option></select></div>
      </div>
      <div class="sp"></div>
      <button class="btn ghost" id="refresh">@RC@ Refresh</button>
      <a class="btn outline" href="/export">@EX@ Export</a>
    </div>
    <div class="card">@CARD@</div>
  </main>
</div>
</body></html>"""


def _page(title: str, body: str) -> bytes:
    return (
        _PAGE.replace("@CSS@", _CSS)
        .replace("@TITLE@", title)
        .replace("@RAIL@", _RAIL)
        .replace("@SIDEBAR@", _SIDEBAR)
        .replace("@RC@", _svg("refresh", 14))
        .replace("@EX@", _svg("export", 14))
        .replace("@CARD@", body)
        .encode("utf-8")
    )


# ---------------------------------------------------------------------------
# app + handler
# ---------------------------------------------------------------------------


class App:
    def __init__(self, data_dir: str, gt: str):
        self.data_dir = data_dir
        self.inbox, self.rows, self.details = _build(data_dir)
        for r in self.rows:
            r["ui"] = _ui_metrics(r, self.details[r["email_id"]])
        self.sub = {
            r["email_id"]: {
                k: r[k]
                for k in ("category", "status", "review_reason", "defect_fields", "has_defect")
            }
            for r in self.rows
        }
        self.score = _score_report(self.sub, gt)

    def filtered(self, q) -> list:
        cat = (q.get("cat") or [""])[0]
        status = (q.get("status") or [""])[0]
        text = (q.get("q") or [""])[0].lower()
        out = []
        for r in self.rows:
            if cat and r["category"] != cat:
                continue
            if status and r["status"] != status:
                continue
            if text and text not in " ".join(
                (r["email_id"], r["from"], r["subject"])
            ).lower():
                continue
            out.append(r)
        return out

    def detail(self, eid: str) -> dict:
        row = next(r for r in self.rows if r["email_id"] == eid)
        extra = self.details[eid]
        return {
            "result": row,
            "subject": row["subject"],
            "from": row["from"],
            "body": extra["body"],
            "docs": extra["docs"],
        }

    @property
    def stats(self) -> dict:
        return {
            "total": len(self.rows),
            "by_category": {c: sum(1 for r in self.rows if r["category"] == c) for c in CATEGORIES},
            "ok": sum(1 for r in self.rows if r["status"] == "OK"),
            "mismatch": sum(1 for r in self.rows if r["status"] == "MISMATCH"),
            "review": sum(1 for r in self.rows if r["status"] == "NEEDS_REVIEW"),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "SDOCWebUI/1.0"

    def log_message(self, fmt, *args):  # quieter logs
        return

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, code: int, msg: str):
        self._send(code, msg.encode(), "text/plain; charset=utf-8")

    def do_GET(self):  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path, q = parsed.path, parse_qs(parsed.query)
            if path in ("/", "/index.html"):
                return self._index()
            if path == "/api/stats":
                return self._json(app.stats)
            if path == "/api/emails":
                return self._json(app.filtered(q))
            if path == "/api/score":
                return self._json(app.score or {"error": "no ground truth found"})
            if path == "/score":
                return self._score_page()
            if path == "/export":
                return self._export()
            m = re.fullmatch(r"/email/(\S+)", path)
            if m and m.group(1) in app.details:
                return self._email_page(m.group(1))
            m = re.fullmatch(r"/api/email/(\S+)", path)
            if m and m.group(1) in app.details:
                return self._json(app.detail(m.group(1)))
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self._err(404, "not found")
        except Exception as exc:  # noqa: BLE001
            self._err(500, f"internal error: {exc}")

    # -- pages -------------------------------------------------------------
    def _index(self) -> None:
        st = app.stats
        tabs = ["".join(
            f'<button class="tab active" data-cat=""><div class="n">{st["total"]}</div>'
            '<div class="l">All emails</div></button>'
        )]
        for c in CATEGORIES:
            tabs.append("".join(
                f'<button class="tab" data-cat="{c}"><div class="n">{st["by_category"][c]}</div>'
                f'<div class="l">{c.lower()}</div></button>'
            ))
        tabs.append(
            f'<button class="tab amber" data-status="NEEDS_REVIEW"><div class="n">{st["review"]}</div>'
            '<div class="l">Drafts · review</div></button>'
        )
        fil = (
            '<div class="filters">'
            '<div class="field fsearch"><span class="fi">{sg}</span>'
            '<input id="f-q" placeholder="Search emails…"></div>'
            '<div class="field"><span>{cl}</span>Date range · All time</div>'
            f'<select class="field" id="f-cat"><option value="">All categories</option>'
            + "".join(f'<option value="{c}">{c}</option>' for c in CATEGORIES)
            + "</select>"
            f'<select class="field" id="f-status"><option value="">All statuses</option>'
            + "".join(f'<option value="{s}">{s}</option>' for s in STATUSES)
            + "</select>"
            '<div class="sp"></div>'
            '<button class="morefilters" id="f-more">{sld} More filters</button>'
            "</div>"
            '<div class="help">Showing <b id="count">0</b> of '
            f'<b>{st["total"]}</b> emails · data: <b>{_esc(app.data_dir)}</b></div>'
        ).format(sg=_svg("search", 14), cl=_svg("calendar", 14), sld=_svg("sliders", 14))
        table = """
<table><thead><tr>
<th class="th-email">Email</th><th>Category</th><th>Status</th><th>Reason</th><th>Source</th>
<th>Performance</th><th>Fidelity</th><th class="th-end">Actions</th>
</tr></thead><tbody id="rows"></tbody></table>"""
        src_txt = _esc(os.path.basename(app.data_dir.rstrip("/\\")) or (app.data_dir if len(app.data_dir) <= 12 else app.data_dir[:12]))
        file_icon = _svg("clip", 13)
        dots_icon = _svg("more", 18)
        script = """
<script>
const esc=s=>(s+'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ICON_DOTS=`@@DOTS@@`,ICON_FILE=`@@FILE@@`,SRC=@@SRC@@;
const toneColor=t=>({'teal':'var(--teal)','green':'var(--green)','amber':'var(--amber)','grey':'#B6C0CC','rose':'#e0483e'}[t]||'var(--teal)');
let data=[], state={cat:'',status:'',q:''};
async function load(){data=await (await fetch('/api/emails')).json();render();}
function dots(id){return `<button class="threed" title="view details" onclick="location='/email/${id}'">${ICON_DOTS}</button>`;}
function render(){
  const rows=data.filter(r=>state.cat?r.category===state.cat:true)
    .filter(r=>state.status?r.status===state.status:true)
    .filter(r=>state.q?(r.email_id+' '+r.from+' '+r.subject).toLowerCase().includes(state.q):true);
  document.getElementById('count').textContent=rows.length;
  document.querySelectorAll('.tab').forEach(t=>{
    const on=(state.status&&t.dataset.status===state.status)||(state.cat&&t.dataset.cat===state.cat)||(!state.status&&!state.cat&&!t.dataset.cat);
    t.classList.toggle('active',on);});
  const el=document.getElementById('rows');
  el.innerHTML=rows.map(r=>{
    const u=r.ui;
    const pills=u.pills.map(p=>`<span class="pill ${p[1]}">${esc(p[0])}</span>`).join('');
    const chans=Array.from({length:Math.max(1,u.channels)}).fill(ICON_FILE).join('&nbsp;&nbsp;');
    const metrics=u.metrics.map(m=>`<div class="met"><div class="mt"><span>${esc(m[0])}</span><b>${esc(m[1])}</b></div>`+
      `<div class="bar"><i style="width:${m[2]}%;background:${toneColor(m[3])}"></i></div></div>`).join('');
    const stClass=r.status==='OK'?'green':(r.status==='MISMATCH'?'red':'amber');
    const reason=r.review_reason||(r.status==='OK'&&r.category==='BL_COMPARISON'?'checked':'-');
    return `<tr><td class="cel-name"><a class="cname" href="/email/${r.email_id}">${esc(r.email_id)}</a>`+
      `<div class="chan">${chans}</div><div class="pills">${pills}</div></td>`+
      `<td class="cell-txt">${esc(r.category)}</td>`+
      `<td class="cell-txt"><span class="dot ${stClass}"></span>${esc(r.status)}</td>`+
      `<td class="cell-mut">${esc(reason)}</td>`+
      `<td class="cell-mut">${SRC}</td>`+
      `<td style="min-width:170px">${metrics}</td>`+
      `<td style="min-width:110px"><div class="fid" style="color:${toneColor(u.fid[2])}">${esc(u.fid[0])}`+
      `<div class="cap">${esc(u.fid[1])}</div></div></td>`+
      `<td style="text-align:right">${dots(r.email_id)}</td></tr>`;}).join('');
}
function applySideFilter(kind,value){if(kind==='all'){state.cat='';state.status='';}
  else{state[kind]=value;state[kind==='cat'?'status':'cat']='';}render();}
document.addEventListener('DOMContentLoaded',()=>{
  document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
    if(t.dataset.status){state.status=t.dataset.status;state.cat='';}
    else if(t.dataset.cat!==undefined){state.cat=t.dataset.cat;state.status='';}
    else{state.cat='';state.status='';}
    render();}));
  const wire=(id,key)=>document.getElementById(id).addEventListener('input',e=>{state[key]=e.target.value.toLowerCase();render();});
  wire('side-search','q');wire('f-q','q');wire('f-status','status');
  document.getElementById('f-cat').addEventListener('change',e=>{state.cat=e.target.value;state.status='';render();});
  document.querySelectorAll('.sitem[data-cat]').forEach(s=>s.addEventListener('click',()=>applySideFilter('cat',s.dataset.cat)));
  document.querySelectorAll('.sitem[data-status]').forEach(s=>s.addEventListener('click',()=>applySideFilter('status',s.dataset.status)));
  document.querySelectorAll('.sitem[data-all]').forEach(s=>s.addEventListener('click',()=>applySideFilter('all',1)));
  document.querySelectorAll('.sitem[data-score]').forEach(s=>s.addEventListener('click',()=>location='/score'));
  document.querySelectorAll('.rail .ico[data-cat]').forEach(b=>b.addEventListener('click',()=>applySideFilter('cat',b.dataset.cat)));
  document.querySelectorAll('.rail .ico[data-status]').forEach(b=>b.addEventListener('click',()=>applySideFilter('status',b.dataset.status)));
  document.querySelectorAll('.rail .ico[data-tab]').forEach(b=>b.addEventListener('click',()=>applySideFilter('all',1)));
  document.getElementById('refresh').addEventListener('click',()=>location.reload());
  load();
});
</script>"""
        script = (
            script
            .replace("@@DOTS@@", dots_icon.replace("\\", "\\\\"))
            .replace("@@FILE@@", file_icon.replace("\\", "\\\\"))
            .replace("@@SRC@@", json.dumps(src_txt))
        )
        body = "<div class=\"tabs\">" + "".join(tabs) + "</div>" + fil + table + script
        self._send(200, _page("Dashboard", body), "text/html; charset=utf-8")

    def _email_page(self, eid: str) -> None:
        d = app.detail(eid)
        res = d["result"]
        u = res["ui"]
        docs = ""
        for doc in d["docs"]:
            kind = {
                "shipping_instruction": "Shipping Instruction",
                "bill_of_lading": "Bill of Lading",
                "other": "other",
                "error": "unreadable",
            }.get(str(doc["kind"]), str(doc["kind"]))
            read = doc["readable"] or "unreadable"
            fields = "".join(
                f'<div class="keyval"><b>{_esc(f)}:</b> '
                f'{_esc(doc["fields"].get(f, "") or "—")}</div>' for f in FIELDS
            )
            docs += (
                '<div class="card" style="padding:16px 20px">'
                f'<h3>{_esc(doc["path"])} · {_esc(kind)} · {_esc(read)}</h3>'
                f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:6px 16px">{fields}</div>'
                f'<details style="margin-top:10px"><summary>raw text ({len(doc["text"])} chars)</summary>'
                f'<div class="fmt">{_esc(doc["text"])}</div></details></div>'
            )
        mm = "".join(
            f'<div class="met"><div class="mt"><span>{_esc(m[0])}</span><b>{_esc(m[1])}</b></div>'
            f'<div class="bar"><i style="width:{m[2]}%;background:{_svg_color(m[3])}"></i></div></div>'
            for m in u["metrics"]
        )
        chips = "".join(f'<span class="chip">{_esc(f)}</span>' for f in res["defect_fields"])
        head = (
            f'<div class="dtitle">{_esc(eid)} <span style="font-weight:400;color:var(--muted);font-size:14px">· {_esc(d["from"])}</span></div>'
            '<div class="subrow">'
            f'<span>Category: <b>{_esc(res["category"])}</b></span>'
            f'<span>Status: <b>{_esc(res["status"])}</b></span>'
            f'<span class="cell-mut">Reason: {_esc(res["review_reason"] or "—")}</span>'
            f'<span class="cell-mut">has_defect: <b>{_esc(res["has_defect"])}</b></span>'
            f'<span class="cell-mut">confidence: <b>{_esc(res["confidence"])}</b></span>'
            "</div>"
        )
        body = (
            f'<a href="/" style="font-size:12.5px">‹ back to dashboard</a>'
            '<div class="card" style="padding:20px 28px">' + head +
            f'<h3>Subject</h3><div class="keyval">{_esc(d["subject"])} {chips}</div>'
            f'<details style="margin-top:8px"><summary>Body</summary><div class="fmt">{_esc(d["body"])}</div></details>'
            "</div>"
            '<div class="card" style="padding:20px 28px"><h3>Pipeline</h3>' + mm + "</div>"
            + docs
        )
        self._send(200, _page(eid, body), "text/html; charset=utf-8")

    def _score_page(self) -> None:
        sc = app.score
        if not sc:
            return self._err(404, "no ground truth file; pass --gt data/ground_truth.json")
        s1, s3, ee, rl = sc["stage1"], sc["stage3"], sc["end_to_end"], sc["reliability"]
        grid = (
            '<div class="metgrid">'
            f'<div><div class="fid" style="color:var(--teal)">{sc["final_score"]}<div class="cap">Final score</div></div></div>'
            f'<div><div class="fid" style="color:var(--teal)">{s1["macro_f1"]}<div class="cap">Stage-1 macro-F1</div></div></div>'
            f'<div><div class="fid" style="color:var(--green)">{s3["defect_f1"]}<div class="cap">Stage-3 defect-F1</div></div></div>'
            f'<div><div class="fid" style="color:var(--green)">{ee["rate"]}<div class="cap">End-to-end rate</div></div></div>'
            f'<div><div class="fid" style="color:var(--amber)">{rl["escalation_f1"]}<div class="cap">Escalation F1</div></div></div>'
            "</div>"
        )
        body = (
            '<a href="/" style="font-size:12.5px">‹ back to dashboard</a>'
            '<div class="card" style="padding:20px 28px"><h3>Score summary</h3>' + grid + "</div>"
            '<div class="card" style="padding:20px 28px"><h3>Full report</h3>'
            '<div class="fmt">' + _esc(json.dumps(sc, indent=2, ensure_ascii=False)) + "</div></div>"
        )
        self._send(200, _page("Score", body), "text/html; charset=utf-8")

    def _export(self) -> None:
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


def _svg_color(tone: str) -> str:
    return {"teal": "#00A5A5", "green": "#3BB273", "amber": "#F5A623",
            "grey": "#B6C0CC", "rose": "#e0483e"}.get(tone, "#00A5A5")


app: App = None  # set in main()


def main() -> int:
    parser = argparse.ArgumentParser(description="Web UI for the SDOC pipeline")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--gt", default=os.path.join("data", "ground_truth.json"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()

    global app
    print(f"building pipeline over {args.data_dir} ...")
    app = App(args.data_dir, args.gt)
    if app.score:
        s = app.score
        print(f"local score: final={s['final_score']} stage1_macro_f1={s['stage1']['macro_f1']} "
              f"defect_f1={s['stage3']['defect_f1']} end_to_end={s['end_to_end']['rate']}")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"SDOC web UI: http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())