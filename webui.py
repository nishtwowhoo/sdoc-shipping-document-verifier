"""Minimal dependency-free web UI for the SDOC pipeline.

Two switchable themes share one server/data pipeline:

  - modern (default): polished light SaaS dashboard (KPI cards, sticky
    filters, SI-vs-BL diff view, scorecards).
  - classic: the original three-column dashboard (unchanged output).

Run from the project root:

    python webui.py                     # modern theme, port 8081
    python webui.py --theme classic     # original look
    python webui.py --theme modern --port 8082

Switch themes live in the browser with the button in the top bar (sets a
cookie), or via the query arg ?theme=classic. Open http://127.0.0.1:8081
(default port avoids the docker server on 8080).
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

from classify import CATEGORIES, classify
from comparator import REVIEW_REASONS, analyze_email, _parse_doc
from extractor import FIELDS, is_placeholder
from loader import Inbox
import hitl
import actions

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
                tone = "green" if nf == 7 else ("amber" if nf >= 5 else "rose")
                metrics.append(("Extracted", f"{nf}/7", int(nf / 7 * 100), tone))
        if st == "MISMATCH":
            n = len(r["defect_fields"])
            metrics.append(("Defects", str(n), min(100, n * 15), "rose" if n else "green"))
        metrics = metrics[:3]

        if st == "OK":
            fid = ("100%", "Fields match", "green")
        elif st == "MISMATCH":
            fid = ("100%", f"{len(r['defect_fields'])} defect(s) flagged", "rose")
        else:
            fid = ("100%", (r["review_reason"] or "escalated").replace("_", " "), "amber")
        return {"pills": pills, "channels": n_docs, "metrics": metrics, "fid": fid}

    pct = round(conf * 100)
    col = "green" if pct >= 80 else "amber"
    metrics = [("Confidence", f"{pct}%", pct, col), ("Rules", "1/1", 100, "green")]
    fid = {
        "SI_REQUEST": (f"{pct}%", "Routing confidence", "green"),
        "INVOICE_QUERY": (f"{pct}%", "Matching", "green"),
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


def _fmt(v) -> str:
    """Format an extracted field value for display."""
    if v is None or v == "":
        return ""
    return str(v)


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
    "swap": '<path d="M7 5h12M15 8l3-3-3-3"/><path d="M17 19H5M9 22l-3-3 3-3"/>',
    "panel": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10"/>',
    "layers": '<path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/>',
    "zoom": '<circle cx="11" cy="11" r="6.5"/><path d="M16.5 16.5L21 21"/><path d="M8.5 11h5M11 8.5v5"/>',
    "eye": '<path d="M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.8"/>',
    "chart": '<path d="M4 19V5"/><path d="M4 19h16"/><path d="M8 16l3-4 3 2 5-7"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "spam": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/><path d="M6 18l12-12"/>',
    "arrow-left": '<path d="M19 12H5M11 18l-6-6 6-6"/>',
}


def _svg(name: str, size: int, cls: str = "",
         stroke: str = "currentColor", fill: str = "none", sw: float = 1.8) -> str:
    inner = _ICONS[name]
    if name in ("more", "user", "x"):
        fill = "currentColor"
    style = f" class=\"{cls}\"" if cls else ""
    return (f'<svg viewBox="0 0 24 24" width="{size}" height="{size}"{style} '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" '
            'stroke-linecap="round" stroke-linejoin="round">' + inner + "</svg>")


def _svg_color(tone: str) -> str:
    return {"teal": "#00A5A5", "green": "#3BB273", "amber": "#F5A623",
            "grey": "#B6C0CC", "rose": "#e0483e"}.get(tone, "#00A5A5")


_HEX = {
    "teal": "#06A6A6", "green": "#3BB273", "amber": "#E8971B",
    "grey": "#94A3B8", "rose": "#E4574D",
}

_KIND_LABEL = {
    "shipping_instruction": "Shipping Instruction",
    "bill_of_lading": "Bill of Lading",
    "other": "other",
    "error": "unreadable",
}


class App:
    def __init__(self, data_dir: str, gt: str):
        self.data_dir = data_dir
        self.inbox, self.rows, self.details = _build(data_dir)
        self.store = hitl.Store(data_dir)
        self.action_store = actions.ActionStore(data_dir)
        self._explain_cache: dict = {}
        self._apply_overrides()
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

    def _apply_overrides(self) -> None:
        try:
            overrides = self.store.all()
        except Exception:
            overrides = {}
        self.overrides = overrides or {}
        for r in self.rows:
            ov = self.overrides.get(r["email_id"])
            if not ov:
                continue
            if ov.get("resolved_category"):
                r["category"] = ov["resolved_category"]
            if ov.get("resolved_status"):
                r["status"] = ov["resolved_status"]
                if ov["resolved_status"] != "NEEDS_REVIEW":
                    r["review_reason"] = None

    def resolve(self, email_id: str, payload: dict) -> dict:
        row = next(r for r in self.rows if r["email_id"] == email_id)
        extra = self.details[email_id]
        ai = payload.get("ai") or {}
        record = {
            "email_id": email_id,
            "orig_category": payload.get("orig_category", row["category"]),
            "orig_status": payload.get("orig_status", row["status"]),
            "review_reason": row.get("review_reason"),
            "resolved_category": payload.get("resolved_category", row["category"]),
            "resolved_status": payload.get("resolved_status", "OK"),
            "ai_explanation": ai.get("explanation", ""),
            "ai_fix": ai.get("suggested_fix", ""),
            "resolved_by": payload.get("resolved_by", "human"),
            "note": payload.get("note", ""),
        }
        saved = self.store.save(record)
        row["category"] = record["resolved_category"]
        row["status"] = record["resolved_status"]
        if row["status"] != "NEEDS_REVIEW":
            row["review_reason"] = None
        row["ui"] = _ui_metrics(row, extra)
        self.overrides[email_id] = record
        self.sub[email_id] = {
            k: row[k]
            for k in ("category", "status", "review_reason", "defect_fields", "has_defect")
        }
        return saved

    def _refresh_row(self, email_id: str) -> None:
        """Recompute one row from the pipeline, re-applying any override."""
        _, fresh_rows, fresh_details = _build(self.data_dir)
        fresh = next(r for r in fresh_rows if r["email_id"] == email_id)
        self.details[email_id] = fresh_details[email_id]
        ov = self.overrides.get(email_id)
        if ov:
            if ov.get("resolved_category"):
                fresh["category"] = ov["resolved_category"]
            if ov.get("resolved_status"):
                fresh["status"] = ov["resolved_status"]
                if ov["resolved_status"] != "NEEDS_REVIEW":
                    fresh["review_reason"] = None
        fresh["ui"] = _ui_metrics(fresh, self.details[email_id])
        for i, r in enumerate(self.rows):
            if r["email_id"] == email_id:
                self.rows[i] = fresh
                break
        self.sub[email_id] = {
            k: fresh[k]
            for k in ("category", "status", "review_reason", "defect_fields", "has_defect")
        }

    def revert(self, email_id: str) -> dict:
        """Delete a HITL override; email returns to pipeline NEEDS_REVIEW."""
        saved = self.store.delete(email_id)
        self.overrides.pop(email_id, None)
        self._explain_cache.pop(email_id, None)
        self._refresh_row(email_id)
        return saved

    def reset_all(self) -> dict:
        saved = self.store.clear()
        self.overrides = {}
        self._explain_cache = {}
        _, fresh_rows, fresh_details = _build(self.data_dir)
        self.rows = fresh_rows
        self.details = fresh_details
        for r in self.rows:
            r["ui"] = _ui_metrics(r, self.details[r["email_id"]])
        self.sub = {
            r["email_id"]: {
                k: r[k]
                for k in ("category", "status", "review_reason", "defect_fields", "has_defect")
            }
            for r in self.rows
        }
        return saved

    def draft(self, email_id: str) -> dict:
        row = next(r for r in self.rows if r["email_id"] == email_id)
        d = self.detail(email_id)
        out = actions.build_draft(row, d)
        out["log"] = self.action_store.for_email(email_id)
        if row.get("status") == "MISMATCH":
            out["resolve_suggest"] = {"category": row["category"], "status": "OK"}
        else:
            out["resolve_suggest"] = {"category": "GENERAL", "status": "OK"}
        return out

    def approve_draft(self, payload: dict) -> dict:
        return self.action_store.save({
            "email_id": payload.get("email_id", ""),
            "action": payload.get("action", "approve_and_reply"),
            "draft_to": payload.get("to", ""),
            "draft_subject": payload.get("subject", ""),
            "draft_body": payload.get("body", ""),
            "approved_by": payload.get("approved_by", "human"),
        })

    def explain(self, email_id: str, question: str | None = None) -> dict:
        row = next(r for r in self.rows if r["email_id"] == email_id)
        if not question and email_id in self._explain_cache:
            out = dict(self._explain_cache[email_id])
            out["backend"] = self.store.backend
            out["cached"] = True
            return out
        extra = self.details[email_id]
        email = {
            "email_id": email_id,
            "subject": row.get("subject", ""),
            "from": row.get("from", ""),
            "body": extra.get("body", ""),
        }
        out = hitl.gemini_explain(email, extra.get("docs", []), row.get("review_reason"), question)
        out["backend"] = self.store.backend
        out["review_reason"] = row.get("review_reason")
        if not question and out.get("source") == "gemini":
            self._explain_cache[email_id] = dict(out)
        return out

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


# ---------------------------------------------------------------------------
# HITL AI agent card (NEEDS_REVIEW only, email detail page)
# ---------------------------------------------------------------------------


def _hitl_card(eid: str, review_reason: str | None) -> str:
    return f"""
<div class="card" id="hitl-card">
<style>#hitl-card .ai-body{{font-size:13.5px;line-height:1.65;color:var(--text,#222B36)}}
#hitl-card .ai-body ul{{margin:6px 0;padding-left:20px}}
#hitl-card .ai-body li{{margin:3px 0}}
#hitl-card .ai-body code{{background:#EEF1F5;border-radius:6px;padding:1px 6px;font-size:12px}}</style>
<h3>AI agent · human-in-the-loop</h3>
<div class="cell-mut">Reason: <b>{_esc(review_reason or "—")}</b> · Ask what is wrong with this email.</div>
<div id="hitl-out" style="margin-top:10px"><div class="cell-mut">Loading AI explanation…</div></div>
<div id="hitl-thread" style="margin-top:6px"></div>
<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
<input id="hitl-q" placeholder="Ask a follow-up, e.g. what file is missing?" style="flex:1;min-width:220px;height:37px;border:1px solid var(--line);border-radius:11px;padding:0 12px">
<button class="btn outline" id="hitl-ask">Ask AI</button>
</div>
<div class="cell-mut" style="margin-top:12px">To close this review, use <b>Resolve</b> in the Action engine card below — the AI suggestion pre-fills it.</div>
</div>
<script>
(function(){{
const eid={json.dumps(eid)};
let lastAI=null;
const out=document.getElementById('hitl-out');
const esc=s=>(s+'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const md=s=>{{
  let h=esc(s||'—');
  h=h.replace(/`([^`]+)`/g,'<code>$1</code>');
  h=h.replace(/\\*\\*([^*]+)\\*\\*/g,'<strong>$1</strong>');
  const lines=h.split(/\\n/), out=[], list=[];
  const flush=()=>{{ if(list.length){{ out.push('<ul>'+list.join('')+'</ul>'); list.length=0; }} }};
  for(const ln of lines){{
    const m=ln.match(/^\\s*(?:\\*|-|•)\\s+(.*)$/);
    if(m) list.push('<li>'+m[1]+'</li>');
    else{{ flush(); if(ln.trim()) out.push(ln); }}
  }}
  flush();
  return '<div class="ai-body">'+out.join('<br>')+'</div>';
}};
async function explain(question){{
  const thread=document.getElementById('hitl-thread');
  if(!question) out.innerHTML='<div class="cell-mut">Thinking…</div>';
  else thread.innerHTML+='<div class="keyval" style="margin-top:8px"><b>You:</b> '+esc(question)+'</div><div class="cell-mut">Thinking…</div>';
  const url='/api/hitl/explain/'+encodeURIComponent(eid)+(question?'?q='+encodeURIComponent(question):'');
  const r=await fetch(url); const j=await r.json();
  if(j.error||j.source==='error'){{
    const html='<div class="keyval"><b>AI unavailable</b></div><div style="margin:4px 0">'+esc(j.explanation||'AI ran into an error.')+'</div>';
    if(!question) out.innerHTML=html;
    else thread.innerHTML=thread.innerHTML.replace('<div class="cell-mut">Thinking…</div>',html);
    return;
  }}
  if(j.answer){{
    thread.innerHTML=thread.innerHTML.replace('<div class="cell-mut">Thinking…</div>',
      '<div style="margin:4px 0">'+md(j.answer)+'</div>');
    return;
  }}
  lastAI=j; window._hitlAI=j;
  const _cat=document.getElementById('act-cat'), _st=document.getElementById('act-status');
  if(_cat) _cat.value=j.suggested_category||'GENERAL';
  if(_st) _st.value=j.suggested_status||'OK';
  out.innerHTML='<div class="keyval"><b>What is wrong</b></div><div style="margin:4px 0 10px">'+md(j.explanation||'—')+'</div>'
    +'<div class="keyval"><b>Suggested fix</b></div><div style="margin:4px 0">'+md(j.suggested_fix||'—')+'</div>'
    +'<div class="cell-mut" style="margin-top:6px">source: '+esc(j.source||'?')+' · backend: '+esc(j.backend||'?')+' · reason: '+esc(j.review_reason||'—')+'</div>';
}}
document.getElementById('hitl-ask').addEventListener('click',()=>{{
  const box=document.getElementById('hitl-q');
  const val=box.value.trim();
  if(!val) return;
  box.value='';
  explain(val);
}});
explain('');
}})();
</script>"""


def _action_card(eid: str, status: str) -> str:
    cats = "".join(f'<option value="{c}">{c}</option>' for c in CATEGORIES)
    stats = "".join(f'<option value="{s}">{s}</option>' for s in STATUSES)
    title = ("Amendment request" if status == "MISMATCH"
             else "Follow-up request")
    return f"""
<div class="card" id="action-card">
<h3>Action engine · {title}</h3>
<div class="cell-mut">Auto-drafted from this email's pipeline facts. Review, edit, then approve.</div>
<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
<input id="act-to" placeholder="To" style="flex:1;min-width:180px;height:37px;border:1px solid var(--line);border-radius:11px;padding:0 12px">
<input id="act-subject" placeholder="Subject" style="flex:2;min-width:220px;height:37px;border:1px solid var(--line);border-radius:11px;padding:0 12px">
</div>
<textarea id="act-body" rows="10" style="width:100%;margin-top:8px;border:1px solid var(--line);border-radius:11px;padding:10px 12px;font:13px/1.6 inherit">Loading draft…</textarea>
<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
<button class="btn ghost" id="act-copy">Copy</button>
<button class="btn ghost" id="act-mailto">Open in mail app</button>
<button class="btn outline" id="act-approve">Approve &amp; log reply</button>
</div>
<div class="cell-mut" id="act-msg" style="margin-top:8px"></div>
<div id="act-log" class="cell-mut" style="margin-top:4px"></div>
<div style="margin-top:14px;padding-top:14px;border-top:1px solid var(--line)">
<h3>Resolve this review</h3>
<div style="display:flex;gap:8px;flex-wrap:wrap">
<select class="field" id="act-cat">{cats}</select>
<select class="field" id="act-status">{stats}</select>
<button class="btn outline" id="act-resolve">Resolve</button>
</div>
</div>
</div>
<script>
(function(){{
const eid={json.dumps(eid)};
const msg=document.getElementById('act-msg');
const toEl=document.getElementById('act-to');
const subEl=document.getElementById('act-subject');
const bodyEl=document.getElementById('act-body');
async function load(){{
  const r=await fetch('/api/draft/'+encodeURIComponent(eid));
  const j=await r.json();
  toEl.value=j.to||''; subEl.value=j.subject||''; bodyEl.value=j.body||'';
  document.getElementById('act-cat').value=(j.resolve_suggest&&j.resolve_suggest.category)||'GENERAL';
  document.getElementById('act-status').value=(j.resolve_suggest&&j.resolve_suggest.status)||'OK';
  const log=j.log||[];
  if(log.length) document.getElementById('act-log').textContent=log.length+' approved repl'+(log.length>1?'ies':'y')+' logged for this email.';
}}
document.getElementById('act-copy').addEventListener('click',async ()=>{{
  const txt='To: '+toEl.value+'\\nSubject: '+subEl.value+'\\n\\n'+bodyEl.value;
  try{{ await navigator.clipboard.writeText(txt); msg.textContent='Copied to clipboard.'; }}
  catch(e){{ bodyEl.select(); document.execCommand('copy'); msg.textContent='Copied (fallback).'; }}
}});
document.getElementById('act-mailto').addEventListener('click',()=>{{
  location.href='mailto:'+encodeURIComponent(toEl.value)
    +'?subject='+encodeURIComponent(subEl.value)
    +'&body='+encodeURIComponent(bodyEl.value);
}});
document.getElementById('act-approve').addEventListener('click',async ()=>{{
  if(!confirm('Approve this reply for '+eid+'? It will be logged.'))return;
  msg.textContent='Logging…';
  const r=await fetch('/api/draft/approve',{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{email_id:eid,to:toEl.value,subject:subEl.value,body:bodyEl.value}})}});
  const j=await r.json();
  msg.textContent='Approved & logged via '+j.backend+'. You can now Resolve below.';
  load();
}});
document.getElementById('act-resolve').addEventListener('click',async ()=>{{
  if(!confirm('Resolve '+eid+' with the category/status above?'))return;
  msg.textContent='Resolving…';
  const payload={{email_id:eid,
    resolved_category:document.getElementById('act-cat').value,
    resolved_status:document.getElementById('act-status').value,
    ai:(window._hitlAI||{{}})}};
  const r=await fetch('/api/review',{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify(payload)}});
const j=await r.json();
  msg.textContent='Resolved via '+j.backend+'. Reloading…';
  setTimeout(()=>location.reload(),700);
}});
load();
}})();
</script>"""


def _resolved_banner(eid: str, record: dict, total_resolved: int) -> str:
    return f"""
<div class="card" id="resolved-banner" style="border-color:var(--warn, #E8971B)">
<h3>Human-resolved review</h3>
<div class="cell-mut">Was <b>NEEDS_REVIEW</b> ({_esc(record.get("review_reason") or "—")}) · now
<b>{_esc(record.get("resolved_category"))} / {_esc(record.get("resolved_status"))}</b>
by {_esc(record.get("resolved_by") or "human")}. Wrong flag? Revert it below.</div>
<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
<button class="btn outline" id="hitl-undo">Undo this review</button>
<button class="btn ghost" id="hitl-reset-all">Reset all {total_resolved} review(s)</button>
</div>
<div class="cell-mut" id="hitl-revert-msg" style="margin-top:8px"></div>
</div>
<script>
(function(){{
const eid={json.dumps(eid)};
const msg=document.getElementById('hitl-revert-msg');
document.getElementById('hitl-undo').addEventListener('click',async ()=>{{
  if(!confirm('Revert '+eid+' back to NEEDS_REVIEW?'))return;
  msg.textContent='Reverting…';
  const r=await fetch('/api/revert',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email_id:eid}})}});
  const j=await r.json();
  msg.textContent='Reverted via '+j.backend+'. Reloading…';
  setTimeout(()=>location.reload(),700);
}});
document.getElementById('hitl-reset-all').addEventListener('click',async ()=>{{
  if(!confirm('Reset ALL human reviews back to NEEDS_REVIEW?'))return;
  msg.textContent='Resetting…';
  const r=await fetch('/api/revert-all',{{method:'POST'}});
  const j=await r.json();
  msg.textContent='Reset via '+j.backend+'. Reloading…';
  setTimeout(()=>location.reload(),900);
}});
}})();
</script>"""


# ---------------------------------------------------------------------------
# themes
# ---------------------------------------------------------------------------


class Theme:
    name = "theme"
    other = "theme"
    switch_label = "Switch UI"

    def switcher(self) -> str:
        return (f'<a class="btn ghost" href="/?theme={self.other}" title="Try the other UI">'
                f'{_svg("swap", 14)}<span>{self.switch_label}</span></a>')

    def shell(self, title: str, body: str) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def render_index(self, app: App) -> str:  # pragma: no cover
        raise NotImplementedError

    def render_email(self, app: App, eid: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def render_score(self, app: App) -> str:  # pragma: no cover
        raise NotImplementedError


# ===========================================================================
# CLASSIC THEME (original look, byte-stable)
# ===========================================================================

_CLASSIC_CSS = """
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

_CLASSIC_RAIL = (
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

_CLASSIC_SIDEBAR_LINKS = [
    ("Product", "Inbox overview", "all", "all"),
    ("Product", "BL comparisons", "cat", "BL_COMPARISON"),
    ("Product", "SI requests", "cat", "SI_REQUEST"),
    ("Product", "Invoice queries", "cat", "INVOICE_QUERY"),
    ("Product", "Spam review", "cat", "SPAM"),
    ("Product", "Edge cases", "status", "NEEDS_REVIEW"),
]

_CLASSIC_SIDEBAR = """
<div class="wordmark">Waybill Copilot</div>
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
                  else f' data-{kind}="{value}"'),
            label=label,
        )
        for _, label, kind, value in _CLASSIC_SIDEBAR_LINKS
    ),
)

_CLASSIC_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@TITLE@ · SDOC</title>
<style>@CSS@</style></head><body>
<div class="app">
  <nav class="rail">@RAIL@</nav>
  <nav class="side">@SIDEBAR@</nav>
  <main class="main">
    <div class="topbar">
      <div>
        <div class="breadcrumb">Waybill Copilot <b>›</b> All emails</div>
        <div class="title">Inbox</div>
      </div>
      <div class="sp"></div>
      @SW@
      <button class="btn ghost" id="refresh">@RC@ Refresh</button>
      <a class="btn outline" href="/export">@EX@ Export</a>
    </div>
    <div class="card">@CARD@</div>
  </main>
</div>
</body></html>"""


class ClassicTheme(Theme):
    name = "classic"
    other = "modern"
    switch_label = "Modern UI"

    def switcher(self) -> str:
        return (f'<a class="btn outline" href="/?theme={self.other}" title="Try the new UI">'
                f'{_svg("swap", 14)}<span>{self.switch_label}</span></a>')

    def shell(self, title: str, body: str) -> bytes:
        return (
            _CLASSIC_PAGE.replace("@CSS@", _CLASSIC_CSS)
            .replace("@TITLE@", title)
            .replace("@RAIL@", _CLASSIC_RAIL)
            .replace("@SIDEBAR@", _CLASSIC_SIDEBAR)
            .replace("@SW@", self.switcher() if dev_mode else "")
            .replace("@RC@", _svg("refresh", 14))
            .replace("@EX@", _svg("export", 14))
            .replace("@CARD@", body)
            .encode("utf-8")
        )

    def render_index(self, app: App) -> str:
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
            f'<b>{st["total"]}</b> emails</div>'
        ).format(sg=_svg("search", 14), cl=_svg("calendar", 14), sld=_svg("sliders", 14))
        table = """
<div style="overflow-x:auto"><table><thead><tr>
<th class="th-email">Email</th><th>Category</th><th>Status</th><th>Reason</th>
<th>Performance</th><th>Fidelity</th><th class="th-end">Actions</th>
</tr></thead><tbody id="rows"></tbody></table></div>"""
        file_icon = _svg("clip", 13)
        dots_icon = _svg("more", 18)
        script = """
<script>
const esc=s=>(s+'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ICON_DOTS=`@@DOTS@@`,ICON_FILE=`@@FILE@@`;
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
        )
        body = "<div class=\"tabs\">" + "".join(tabs) + "</div>" + fil + table + script
        return body

    def render_email(self, app: App, eid: str) -> str:
        d = app.detail(eid)
        res = d["result"]
        u = res["ui"]
        docs = ""
        for doc in d["docs"]:
            kind = _KIND_LABEL.get(str(doc["kind"]), str(doc["kind"]))
            read = doc["readable"] or "unreadable"
            fields = "".join(
                f'<div class="keyval"><b>{_esc(f)}:</b> '
                f'{_esc(_fmt(doc["fields"].get(f, "")) or "—")}</div>' for f in FIELDS
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
            + (_resolved_banner(eid, app.overrides[eid], len(app.overrides)) if eid in app.overrides else "")
            + (_hitl_card(eid, res.get("review_reason")) if res.get("status") == "NEEDS_REVIEW" else "")
            + (_action_card(eid, res.get("status")) if res.get("status") in ("MISMATCH", "NEEDS_REVIEW") else "")
            + '<div class="card" style="padding:20px 28px"><h3>Pipeline</h3>' + mm + "</div>"
            + docs
        )
        return body

    def render_score(self, app: App) -> str:
        sc = app.score
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
        return body


# ===========================================================================
# MODERN THEME (polished light SaaS)
# ===========================================================================

_MODERN_CSS = """
:root{
--bg:#EEF2F7;--face:#FFFFFF;--ink:#14203A;--ink-2:#3E4C62;--muted:#64748B;--faint:#94A3B8;
--line:#E6EBF2;--line-strong:#D6DEEA;
--brand:#0E7390;--teal:#06A6A6;--ok:#17A34A;--warn:#E8971B;--bad:#E4574D;--violet:#7C6CF0;
--r-lg:14px;--r-md:10px;--r-sm:8px;
--sh-sm:0 1px 2px rgba(16,34,58,.05);--sh-md:0 8px 22px rgba(16,34,58,.07);--sh-lg:0 20px 44px rgba(16,34,58,.12)}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif;
color:var(--ink);background:var(--bg);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
html{scroll-behavior:smooth}
:focus-visible{outline:2px solid rgba(14,115,144,.4);outline-offset:2px;border-radius:6px}
a{color:var(--brand);text-decoration:none}
h1,h2{font-weight:700;letter-spacing:-.3px}
.sp{flex:1}
.app{display:flex;min-height:100vh}
/* --- sidebar --- */
.side{width:236px;flex:none;background:var(--face);border-right:1px solid var(--line);padding:18px 14px;
display:flex;flex-direction:column;gap:2px;overflow-y:auto;position:sticky;top:0;height:100vh;align-self:flex-start}
.pin{margin-top:auto;flex:none}
.wordmark{font-size:15px;font-weight:800;color:var(--ink);letter-spacing:.2px;display:flex;align-items:center;
gap:9px;padding:4px 8px 18px}
.wordmark .dot{width:9px;height:9px;border-radius:50%;background:linear-gradient(135deg,var(--teal),var(--brand));display:inline-block}
.shead{font-size:10.5px;font-weight:700;letter-spacing:.9px;text-transform:uppercase;color:var(--faint);margin:14px 8px 5px}
.sitem{font-size:13px;font-weight:500;color:var(--ink-2);padding:8px 10px;border-radius:9px;cursor:pointer;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border:0;background:none;text-align:left;width:100%;
display:flex;align-items:center;gap:9px;transition:background .12s,color .12s}
.sitem svg{color:var(--faint)}
.sitem:hover{color:var(--ink);background:#F4F7FB}
.sitem.active{color:var(--brand);background:rgba(14,115,144,.09);font-weight:600}
.sitem.active svg{color:var(--brand)}
/* --- main --- */
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{display:flex;align-items:center;gap:14px;padding:24px 32px 6px;flex-wrap:wrap}
.breadcrumb{font-size:12.5px;color:var(--muted)}
.breadcrumb b{color:var(--ink);font-weight:600}
.title{font-size:22px;font-weight:800;letter-spacing:-.4px;margin:3px 0 0;display:flex;align-items:center;gap:10px}
.btn{height:37px;padding:0 15px;border-radius:11px;font-size:13px;font-weight:600;cursor:pointer;
display:inline-flex;align-items:center;gap:8px;background:var(--face);color:var(--ink-2);
transition:transform .12s,box-shadow .12s,border-color .12s,color .12s}
.btn.ghost{border:1px solid var(--line)}
.btn.ghost:hover{border-color:var(--line-strong);box-shadow:var(--sh-sm);color:var(--ink)}
.btn.outline{color:var(--brand);border:1px solid rgba(14,115,144,.35);background:rgba(14,115,144,.03)}
.btn.outline:hover{background:rgba(14,115,144,.08);box-shadow:var(--sh-sm)}
.btn.sm{height:30px;padding:0 10px;font-size:12px}
.btn.icon{width:37px;padding:0;justify-content:center}
/* --- content + kpis --- */
.content{width:100%;max-width:1180px;margin:0 auto;padding:0 32px 48px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:13px;margin:20px 0 8px}
.kpi{background:var(--face);border:1px solid var(--line);border-radius:var(--r-lg);padding:15px 17px 14px;
cursor:pointer;box-shadow:var(--sh-sm);position:relative;overflow:hidden;text-align:left;transition:transform .13s,box-shadow .13s,border-color .13s}
.kpi:hover{transform:translateY(-2px);box-shadow:var(--sh-md)}
.kpi.active{border-color:var(--brand);box-shadow:0 0 0 3px rgba(14,115,144,.14),var(--sh-sm)}
.kpi .l{font-size:10.5px;font-weight:700;letter-spacing:.9px;text-transform:uppercase;color:var(--muted)}
.kpi .n{font-size:28px;font-weight:800;letter-spacing:-1px;margin:5px 0 9px;font-variant-numeric:tabular-nums;color:var(--ink)}
.kpi .pt{position:absolute;top:15px;right:15px;font-size:11px;font-weight:700;color:var(--faint);font-variant-numeric:tabular-nums}
.kpi .bar{height:5px;border-radius:99px;background:#EDF1F6;overflow:hidden}
.kpi .bar i{display:block;height:100%;border-radius:99px;transition:width .4s ease}
/* --- filters --- */
.filters{display:flex;align-items:center;gap:10px;margin:16px 0 10px;flex-wrap:wrap}
.filter-wrap{position:relative;flex:1 1 220px;min-width:0;max-width:340px}
.filter-wrap .sr{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--faint);display:flex;pointer-events:none}
.filter-wrap input{height:39px;width:100%;border:1px solid var(--line);border-radius:11px;background:#fff;
padding:0 14px 0 38px;font-size:13.5px;color:var(--ink);outline:0;transition:border-color .15s,box-shadow .15s}
.filter-wrap input:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(14,115,144,.12)}
select.field{height:39px;border:1px solid var(--line);border-radius:11px;background:#fff url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6" viewBox="0 0 10 6"><path d="M1 1l4 4 4-4" stroke="%2364748B" fill="none" stroke-width="1.5"/></svg>') no-repeat right 12px center;
padding:0 34px 0 13px;font-size:13.5px;color:var(--ink);cursor:pointer;outline:0;appearance:none;-webkit-appearance:none}
select.field:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(14,115,144,.12)}
.count{font-size:12.5px;color:var(--muted);background:#E7EDF4;border-radius:99px;padding:4px 12px;font-weight:600}
.count b{color:var(--ink)}
.reset{display:none;color:var(--muted);cursor:pointer;border:0;background:none;font-size:12.5px;align-items:center;gap:5px;padding:6px}
.reset.on{display:inline-flex}
.reset:hover{color:var(--bad)}
/* --- table --- */
.tbl{background:var(--face);border:1px solid var(--line);border-radius:var(--r-lg);box-shadow:var(--sh-sm);overflow-x:auto}
table{width:100%;min-width:920px;border-collapse:collapse}
thead th{font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--muted);text-align:left;
padding:14px 18px 12px;border-bottom:1px solid var(--line);background:#FBFCFE;white-space:nowrap}
tbody td{padding:16px 18px;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr{transition:background .12s}
tbody tr:hover td{background:#F7FAFD}
.cid{font-size:13.5px;font-weight:700;color:var(--brand);display:inline-flex;align-items:center;gap:6px}
.cid:hover{color:#0B5E76}
.cell{font-size:13px;color:var(--ink)}
.cat-link{border:0;background:none;padding:0;font-size:13px;font-weight:600;color:var(--brand);cursor:pointer;
text-decoration:underline;text-decoration-color:transparent;text-underline-offset:3px;transition:text-decoration-color .15s}
.cat-link:hover{text-decoration-color:var(--brand)}
.cell-mut{font-size:12.5px;color:var(--muted)}
.chips{display:flex;gap:5px;flex-wrap:wrap;margin-top:7px}
.pill{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;border-radius:99px;padding:3px 9px}
.pill.teal{background:#E1F3F4;color:#0B6E7A}
.pill.green{background:#E4F5EB;color:#176B3E}
.pill.amber{background:#FCEFD7;color:#9A6200}
.pill.grey{background:#EEF1F5;color:#5B6B83}
.pill.rose{background:#FCE9E7;color:#B23B33}
.badge{display:inline-flex;align-items:center;gap:8px;font-size:12.5px;font-weight:600;padding:4px 11px;border-radius:99px}
.badge .dot{width:7px;height:7px;border-radius:50%}
.badge.ok{color:#136B34;background:#E8F6ED}
.badge.ok .dot{background:var(--ok);box-shadow:0 0 0 3px rgba(23,163,74,.18)}
.badge.bad{color:#A83B33;background:#FCEAE7}
.badge.bad .dot{background:var(--bad);box-shadow:0 0 0 3px rgba(228,87,77,.18)}
.badge.warn{color:#8F5B00;background:#FDF2DC}
.badge.warn .dot{background:var(--warn);box-shadow:0 0 0 3px rgba(232,151,27,.18)}
.badge.info{color:#0B6E7A;background:#E4F3F4}
.badge.info .dot{background:var(--teal);box-shadow:0 0 0 3px rgba(6,166,166,.18)}
.act{text-align:right}
.act a{color:var(--muted)}
.act a:hover{color:var(--brand);border-color:var(--line-strong)}
.met{margin-bottom:9px}
.met:last-child{margin-bottom:0}
.met .mt{display:flex;justify-content:space-between;gap:12px;font-size:12px}
.met .mt span{color:var(--muted)}
.met .mt b{font-weight:700;font-variant-numeric:tabular-nums}
.bar{height:5px;border-radius:99px;background:#EDF1F6;margin-top:4px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:99px}
.fid{font-size:18px;font-weight:800;line-height:1.2;letter-spacing:-.3px;font-variant-numeric:tabular-nums}
.fid .cap{font-size:11px;color:var(--muted);font-weight:500;margin-top:3px;text-transform:capitalize}
/* --- cards / detail --- */
.card{background:var(--face);border:1px solid var(--line);border-radius:var(--r-lg);box-shadow:var(--sh-sm);padding:22px 26px;margin-top:18px}
.back{font-size:12.5px;font-weight:600;display:inline-flex;align-items:center;gap:6px;color:var(--muted);margin-bottom:4px}
.back:hover{color:var(--brand)}
.dtitle{font-size:20px;font-weight:800;letter-spacing:-.4px;margin:0 0 3px}
.subrow{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0 4px}
h3{font-size:11.5px;font-weight:700;letter-spacing:.9px;text-transform:uppercase;color:var(--muted);margin:0 0 12px}
.keyval{font-size:13px;color:var(--muted);padding:6px 0;border-bottom:1px dashed var(--line)}
.keyval b{color:var(--ink);font-weight:600}
.metgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:14px}
.sc-card{background:#F7FAFD;border:1px solid var(--line);border-radius:12px;padding:16px}
.sc-card .l{font-size:11px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;color:var(--muted)}
.sc-card .v{font-size:24px;font-weight:800;letter-spacing:-.5px;margin-top:6px;font-variant-numeric:tabular-nums}
.fmt{white-space:pre-wrap;font:12.5px/1.6 ui-monospace,SFMono-Regular,Consolas,Menlo,monospace;background:#0E2540;
color:#D8E6F5;border-radius:11px;padding:14px 16px;max-height:340px;overflow:auto}
details summary{cursor:pointer;font-size:12.5px;font-weight:600;color:var(--ink-2);user-select:none}
details[open] summary{margin-bottom:10px}
.chip{display:inline-flex;align-items:center;gap:5px;background:#EEF1F5;border-radius:99px;padding:3px 10px;font-size:11px;font-weight:600;color:var(--ink-2)}
.chip svg{color:var(--faint)}
/* --- SI vs BL diff --- */
.diff{width:100%;border-collapse:collapse;margin-top:4px}
.diff th,.diff td{padding:11px 14px;text-align:left;vertical-align:top}
.diff thead th{font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--muted);
border-bottom:1px solid var(--line);background:#FBFCFE}
.diff td{border-bottom:1px solid var(--line)}
.diff td.fld{font-weight:700;color:var(--ink);white-space:nowrap}
.diff .val{font-size:13px;color:var(--ink-2);word-break:break-word}
.diff .val .ghost{color:var(--faint);font-style:italic}
.diff .st{font-size:11px;font-weight:700;white-space:nowrap}
.diff .st.okcol{color:#136B34;background:#E8F6ED;padding:3px 10px;border-radius:99px}
.diff .st.badcol{color:#A83B33;background:#FCEAE7;padding:3px 10px;border-radius:99px}
.diff .st.naucol{color:#8F5B00;background:#FDF2DC;padding:3px 10px;border-radius:99px}
.docgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:6px 18px}
@media (max-width:900px){.side{display:none}}
@media (max-width:860px){.kpis{grid-template-columns:repeat(2,1fr)}.filter-wrap{flex:1 1 100%;max-width:none}select.field{flex:1 1 140px;min-width:0}}
@media (max-width:700px){.content{padding:0 16px 40px}.dtitle{font-size:17px}.card{padding:16px}.tbl{border-radius:10px}}
"""

_MODERN_SIDEBAR_LINKS = [
    ("Browse", "Inbox overview", "all", "all", "inbox"),
    ("Browse", "BL comparisons", "cat", "BL_COMPARISON", "swap"),
    ("Browse", "SI requests", "cat", "SI_REQUEST", "doc"),
    ("Browse", "Invoice queries", "cat", "INVOICE_QUERY", "help"),
    ("Browse", "Spam", "cat", "SPAM", "spam"),
    ("Browse", "Needs review", "status", "NEEDS_REVIEW", "shield"),
]

_MODERN_SIDEBAR_GROUPS = []
_prev_head = None
for _row in _MODERN_SIDEBAR_LINKS:
    _MODERN_SIDEBAR_GROUPS.append((_row[0] != _prev_head, _row))
    _prev_head = _row[0]

_MODERN_SIDEBAR = """
<div class="wordmark">Waybill Copilot</div>
{groups}
<div class="pin"></div>
<div class="shead">Actions</div>
<div class="sitem" data-score="1">{sc} Scorecard</div>
<div class="sitem">{ex} Export log</div>
""".format(
    ex=_svg("export", 14),
    sc=_svg("chart", 14),
    groups="".join(
        ('<div class="shead">' + head + "</div>" if first else "") +
        '<div class="sitem{act}"{attr}>{ic}{label}</div>'.format(
            act=" active" if label == "Inbox overview" else "",
            attr=(" data-all=1" if kind == "all"
                  else f' data-{kind}="{value}"'),
            ic=(_svg(icon, 14) + " " if icon else ""),
            label=label,
        )
        for first, (head, label, kind, value, icon) in _MODERN_SIDEBAR_GROUPS
    ),
)

_MODERN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@TITLE@ · SDOC</title>
<style>@CSS@</style></head><body>
<div class="app">
  <nav class="side">@SIDEBAR@</nav>
  <main class="main">
    <div class="topbar">
      <div>
        <div class="breadcrumb">Waybill Copilot <b>›</b> All emails</div>
        <div class="title">Inbox</div>
      </div>
      <div class="sp"></div>
      @SW@
      <button class="btn ghost" id="refresh">@RC@ Refresh</button>
      <a class="btn outline" href="/export">@EX@ Export</a>
    </div>
    <div class="content">@CARD@</div>
  </main>
</div>
</body></html>"""


class ModernTheme(Theme):
    name = "modern"
    other = "classic"
    switch_label = "Classic UI"

    def shell(self, title: str, body: str) -> bytes:
        return (
            _MODERN_PAGE.replace("@CSS@", _MODERN_CSS)
            .replace("@TITLE@", title)
            .replace("@SIDEBAR@", _MODERN_SIDEBAR)
            .replace("@SW@", self.switcher() if dev_mode else "")
            .replace("@RC@", _svg("refresh", 14))
            .replace("@EX@", _svg("export", 14))
            .replace("@CARD@", body)
            .encode("utf-8")
        )

    def render_index(self, app: App) -> str:
        st = app.stats
        total = st["total"] or 1
        cards = [(
            {"all": "1"}, "All emails", str(st["total"]), "#0E7390", True,
        )]
        cat_colors = {
            "BL_COMPARISON": "#06A6A6",
            "SI_REQUEST": "#3BB273",
            "INVOICE_QUERY": "#E8971B",
            "GENERAL": "#7C8AA0",
            "SPAM": "#E4574D",
        }
        cat_labels = {
            "BL_COMPARISON": "BL comparisons",
            "SI_REQUEST": "SI requests",
            "INVOICE_QUERY": "Invoice queries",
            "GENERAL": "General",
            "SPAM": "Spam",
        }
        for c in CATEGORIES:
            cards.append((
                {"cat": c}, cat_labels[c], str(st["by_category"][c]),
                cat_colors[c], False,
            ))
        cards.append((
            {"status": "NEEDS_REVIEW"}, "Needs review", str(st["review"]),
            "#F59E0B", False,
        ))

        def kpi_html(attrs, label, count, color, active):
            pct = int(int(count) / total * 100)
            a = "".join(f' data-{k}="{v}"' for k, v in attrs.items())
            return (
                f'<button class="kpi{" active" if active else ""}"{a} '
                f'style="--k:{color};--kb:{color}" '
                f'><div class="l">{_esc(label)}</div><div class="n">{_esc(count)}</div>'
                f'<div class="pt">{pct}%</div>'
                f'<div class="bar"><i style="width:{pct}%;background:var(--k)"></i></div></button>'
            )

        kpis = '<div class="kpis">' + "".join(
            kpi_html(*c) for c in cards) + "</div>"

        fil = (
            '<div class="filters">'
            '<div class="filter-wrap"><span class="sr">' + _svg("search", 14) + '</span>'
            '<input id="f-q" placeholder="Search emails…"></div>'
            '<select class="field" id="f-cat"><option value="">All categories</option>'
            + "".join(f'<option value="{c}">{c}</option>' for c in CATEGORIES)
            + '</select>'
            '<select class="field" id="f-status"><option value="">All statuses</option>'
            + "".join(f'<option value="{s}">{s}</option>' for s in STATUSES)
            + '</select>'
            '<div class="count">Showing <b id="count">0</b> of ' + str(st["total"]) + '</div>'
            '<div class="sp"></div>'
            '<button class="reset" id="f-reset">' + _svg("x", 12) + ' Clear filters</button>'
            '</div>'
        )

        table = (
            '<div class="tbl"><table><thead><tr>'
            '<th>Email</th><th>Category</th><th>Status</th><th>Reason</th>'
            '<th>Performance</th><th>Fidelity</th><th style="text-align:right">Actions</th>'
            '</tr></thead><tbody id="rows"></tbody></table></div>'
        )

        file_icon = _svg("clip", 13)
        dots_icon = _svg("zoom", 17)
        script = """
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const enc=id=>encodeURIComponent(id);
const ICON_DOTS=`@@DOTS@@`,ICON_FILE=`@@FILE@@`;
const toneColor=t=>({'teal':'#06A6A6','green':'#3BB273','amber':'#E8971B','grey':'#94A3B8','rose':'#E4574D'}[t]||'#06A6A6');
let data=[], state={cat:'',status:'',q:''};
async function load(){data=await (await fetch('/api/emails')).json();render();}
function mkbar(metrics){
  return metrics.map(m=>`<div class="met"><div class="mt"><span>${esc(m[0])}</span><b>${esc(m[1])}</b></div>`+
    `<div class="bar"><i style="width:${m[2]}%;background:${toneColor(m[3])}"></i></div></div>`).join('');
}
function render(){
  const rows=data.filter(r=>state.cat?r.category===state.cat:true)
    .filter(r=>state.status?r.status===state.status:true)
    .filter(r=>state.q?(r.email_id+' '+r.from+' '+r.subject).toLowerCase().includes(state.q):true);
  document.getElementById('count').textContent=rows.length;
  document.querySelectorAll('.kpi').forEach(k=>{
    const ok=(state.status&&k.dataset.status===state.status)||(state.cat&&k.dataset.cat===state.cat)||(!state.status&&!state.cat&&!!k.dataset.all);
    k.classList.toggle('active',ok);});
  const reset=document.getElementById('f-reset');
  reset.classList.toggle('on',!!(state.cat||state.status||state.q));
  document.querySelectorAll('.side .sitem[data-cat],.side .sitem[data-status],.side .sitem[data-all]').forEach(s=>{
    s.classList.toggle('active',!!(s.dataset.cat===state.cat||s.dataset.status===state.status||(s.dataset.all&&!state.cat&&!state.status)));});
  document.getElementById('rows').innerHTML=rows.map(r=>{
    const u=r.ui;
    if(!u||!u.pills) return '';
    const pills=u.pills.map(p=>`<span class="pill ${p[1]}">${esc(p[0])}</span>`).join('');
    const files=Array.from({length:Math.max(1,u.channels)}).map(()=>`<span>${ICON_FILE}</span>`).join('');
    const metrics=mkbar(u.metrics||[]);
    const cls=r.status==='OK'?'ok':(r.status==='MISMATCH'?'bad':'warn');
    const reason=r.review_reason||(r.status==='OK'&&r.category==='BL_COMPARISON'?'checked':'-');
    return `<tr><td><a class="cid" href="/email/${enc(r.email_id)}">${esc(r.email_id)}</a>`+
      `<div class="chips" style="color:var(--faint)">${files} ${pills}</div></td>`+
      `<td class="cell"><button class="cat-link" data-cat="${esc(r.category)}">${esc(r.category)}</button></td>`+
      `<td><span class="badge ${cls}"><span class="dot"></span>${esc(r.status)}</span></td>`+
      `<td class="cell-mut">${esc(reason)}</td>`+
      `<td style="min-width:190px;max-width:230px">${metrics}</td>`+
      `<td style="min-width:120px"><div class="fid" style="color:${toneColor(u.fid[2])}">${esc(u.fid[0])}`+
      `<div class="cap">${esc(u.fid[1])}</div></div></td>`+
      `<td class="act"><a class="btn ghost sm icon" href="/email/${enc(r.email_id)}" title="Inspect ${esc(r.email_id)}">${ICON_DOTS}</a></td></tr>`;
  }).join('');
}
function applyFilter(kind,value){
  if(kind==='all'){state.cat='';state.status='';}
  else{state[kind]=value;state[kind==='cat'?'status':'cat']='';}
  render();
}
document.addEventListener('DOMContentLoaded',()=>{
  document.querySelectorAll('.kpi').forEach(k=>k.addEventListener('click',()=>{
    if(k.dataset.all){state.cat='';state.status='';}
    else if(k.dataset.status){state.status=k.dataset.status;state.cat='';}
    else{state.cat=k.dataset.cat;state.status='';}
    render();}));
  const wire=(id,key)=>document.getElementById(id).addEventListener('input',e=>{state[key]=e.target.value.toLowerCase();render();});
  wire('f-q','q');
  document.getElementById('f-cat').addEventListener('change',e=>{state.cat=e.target.value;state.status='';render();});
  document.getElementById('f-status').addEventListener('change',e=>{state.status=e.target.value;state.cat='';render();});
  document.getElementById('f-reset').addEventListener('click',()=>{state={cat:'',status:'',q:''};
    document.getElementById('f-q').value='';document.getElementById('f-cat').value='';
    document.getElementById('f-status').value='';render();});
  document.querySelectorAll('.sitem[data-cat]').forEach(s=>s.addEventListener('click',()=>applyFilter('cat',s.dataset.cat)));
  document.getElementById('rows').addEventListener('click',e=>{
    const b=e.target.closest('.cat-link');
    if(b) applyFilter('cat',b.dataset.cat);});
  document.querySelectorAll('.sitem[data-status]').forEach(s=>s.addEventListener('click',()=>applyFilter('status',s.dataset.status)));
  document.querySelectorAll('.sitem[data-all]').forEach(s=>s.addEventListener('click',()=>applyFilter('all',1)));
  document.querySelectorAll('.sitem[data-score]').forEach(s=>s.addEventListener('click',()=>location='/score'));
  document.getElementById('refresh').addEventListener('click',()=>location.reload());
  load();
});
</script>"""
        script = (
            script
            .replace("@@DOTS@@", dots_icon.replace("\\", "\\\\"))
            .replace("@@FILE@@", file_icon.replace("\\", "\\\\"))
        )
        return kpis + fil + table + script

    def _diff_rows(self, res, d):
        si = next((x for x in d["docs"] if x["kind"] == "shipping_instruction" and x["readable"]), None)
        bl = next((x for x in d["docs"] if x["kind"] == "bill_of_lading" and x["readable"]), None)
        rows = []
        for f in FIELDS:
            sv = _fmt(si["fields"].get(f)) if si else ""
            bv = _fmt(bl["fields"].get(f)) if bl else ""
            sv_missing = not sv or is_placeholder(sv)
            bv_missing = not bv or is_placeholder(bv)
            if f in res["defect_fields"]:
                st, stc = "MISMATCH", "badcol"
            elif sv_missing or bv_missing:
                st, stc = "MISSING", "naucol"
            else:
                st, stc = "MATCH", "okcol"
            vcell = lambda v, missing: (
                f'<span class="val">{_esc(v)}</span>' if not missing
                else f'<span class="val ghost">{_esc(v or "— not present —")}</span>')
            rows.append(
                f'<tr><td class="fld">{_esc(f.replace("_", " "))}</td>'
                f'<td>{vcell(sv, sv_missing)}</td><td>{vcell(bv, bv_missing)}</td>'
                f'<td><span class="st {stc}">{st}</span></td></tr>'
            )
        return "".join(rows)

    def render_email(self, app: App, eid: str) -> str:
        d = app.detail(eid)
        res = d["result"]
        u = res["ui"]
        cls = "ok" if res["status"] == "OK" else ("bad" if res["status"] == "MISMATCH" else "warn")

        chips = "".join(
            f'<span class="chip">{_svg("flag", 12)} {_esc(f)}</span>'
            for f in res["defect_fields"]
        ) if res["defect_fields"] else ""

        mm = "".join(
            f'<div class="met"><div class="mt"><span>{_esc(m[0])}</span><b>{_esc(m[1])}</b></div>'
            f'<div class="bar"><i style="width:{m[2]}%;background:{_HEX.get(m[3], _HEX["teal"])}"></i></div></div>'
            for m in u["metrics"]
        )

        docs = ""
        for doc in d["docs"]:
            kind = _KIND_LABEL.get(str(doc["kind"]), str(doc["kind"]))
            read = "parsed" if doc["readable"] else "unreadable"
            fields = "".join(
                f'<div class="keyval"><b>{_esc(f.replace("_", " "))}:</b> '
                f'{_esc(_fmt(doc["fields"].get(f, "")) or "—")}</div>' for f in FIELDS
            )
            docs += (
                '<div class="card">'
                f'<h3>{_esc(doc["path"])} · {_esc(kind)} · {_esc(read)}</h3>'
                f'<div class="docgrid">{fields}</div>'
                f'<details style="margin-top:12px"><summary>raw text ({len(doc["text"])} chars)</summary>'
                f'<div class="fmt">{_esc(doc["text"])}</div></details></div>'
            )

        head = (
            f'<a class="back" href="/">{_svg("arrow-left", 12)} Back to dashboard</a>'
            '<div class="card">'
            f'<div class="dtitle">{_esc(eid)} <span style="font-weight:500;color:var(--muted);font-size:14px">· {_esc(d["from"])}</span></div>'
            '<div class="subrow">'
            f'<span class="badge info"><span class="dot"></span>{_esc(res["category"])}</span>'
            f'<span class="badge {cls}"><span class="dot"></span>{_esc(res["status"])}</span>'
            + ('<span class="badge warn"><span class="dot"></span>' + _esc(res["review_reason"] or "") + '</span>' if res["review_reason"] else '')
            + f'<span class="chip">confidence {_esc(res["confidence"])}</span>'
            + chips
            + '</div>'
            '<div style="margin-top:14px;padding-top:14px;border-top:1px solid var(--line)">'
            f'<h3>Subject</h3><div class="keyval" style="border:0;padding:0">{_esc(d["subject"])}</div>'
            '</div>'
            f'<details style="margin-top:12px"><summary>Email body</summary>'
            f'<div class="fmt">{_esc(d["body"])}</div></details>'
            '</div>'
        )

        diff = ""
        if res["category"] == "BL_COMPARISON":
            si = next((x for x in d["docs"] if x["kind"] == "shipping_instruction"), None)
            bl = next((x for x in d["docs"] if x["kind"] == "bill_of_lading"), None)
            if si and bl:
                diff = (
                    '<div class="card"><h3>SI vs draft BL · field comparison</h3>'
                    '<div style="overflow-x:auto">'
                    '<table class="diff"><thead><tr>'
                    '<th>Field</th><th>Shipping Instruction</th><th>Bill of Lading</th><th>Verdict</th>'
                    '</tr></thead><tbody>' + self._diff_rows(res, d) + '</tbody></table></div></div>'
                )

        pipeline = (
            '<div class="card"><h3>Pipeline</h3>' + mm +
            f'<div style="margin-top:14px;padding-top:14px;border-top:1px solid var(--line)">'
            f'<h3>Fidelity</h3><div class="fid" style="color:{_HEX.get(u["fid"][2], _HEX["teal"])}">{_esc(u["fid"][0])}'
            f'<div class="cap">{_esc(u["fid"][1])}</div></div></div></div>'
        )

        hitl_card = _hitl_card(eid, res.get("review_reason")) if res.get("status") == "NEEDS_REVIEW" else ""
        banner = _resolved_banner(eid, app.overrides[eid], len(app.overrides)) if eid in app.overrides else ""
        action = _action_card(eid, res.get("status")) if res.get("status") in ("MISMATCH", "NEEDS_REVIEW") else ""
        return head + banner + hitl_card + action + diff + pipeline + docs

    def render_score(self, app: App) -> str:
        sc = app.score
        s1, s3, ee, rl = sc["stage1"], sc["stage3"], sc["end_to_end"], sc["reliability"]
        grid = (
            '<div class="metgrid">'
            + "".join([
                f'<div class="sc-card"><div class="l">Final score</div>'
                f'<div class="v" style="color:var(--brand)">{sc["final_score"]}</div></div>',
                f'<div class="sc-card"><div class="l">Stage-1 macro-F1</div>'
                f'<div class="v" style="color:var(--brand)">{s1["macro_f1"]}</div></div>',
                f'<div class="sc-card"><div class="l">Stage-3 defect-F1</div>'
                f'<div class="v" style="color:var(--ok)">{s3["defect_f1"]}</div></div>',
                f'<div class="sc-card"><div class="l">End-to-end rate</div>'
                f'<div class="v" style="color:var(--ok)">{ee["rate"]}</div></div>',
                f'<div class="sc-card"><div class="l">Reliability · Escalation F1</div>'
                f'<div class="v" style="color:var(--warn)">{rl["escalation_f1"]}</div></div>',
            ]) + '</div>'
        )
        body = (
            '<a class="back" href="/">' + _svg("arrow-left", 12) + ' Back to dashboard</a>'
            '<div class="card"><h3>Score summary</h3>' + grid + '</div>'
            '<div class="card"><h3>Full report</h3>'
            '<details open><summary>ground-truth evaluation ({0} records)</summary>'.format(len(app.rows)) +
            '<div class="fmt">' + _esc(json.dumps(sc, indent=2, ensure_ascii=False)) + '</div></details>'
            '</div>'
        )
        return body


themes = {"modern": ModernTheme(), "classic": ClassicTheme()}
default_theme = "modern"
dev_mode = False
app: App = None  # set in main()


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "SDOCWebUI/2.0"

    def log_message(self, fmt, *args):  # quieter logs
        return

    # -- helpers ---------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        cookie = getattr(self, "_pending_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, code: int, msg: str):
        self._send(code, msg.encode(), "text/plain; charset=utf-8")

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _theme(self, q) -> Theme:
        q_theme = (q.get("theme") or [""])[0]
        if q_theme in themes:
            self._pending_cookie = f"sdoc_theme={q_theme}; Path=/; Max-Age=31536000; SameSite=Lax"
            return themes[q_theme]
        cookie = self._cookie("sdoc_theme")
        return themes[cookie] if cookie in themes else themes[default_theme]

    def _page(self, theme: Theme, title: str, body: str):
        return theme.shell(title, body)

    # -- routing ---------------------------------------------------------
    def do_GET(self):  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path, q = parsed.path, parse_qs(parsed.query)
            theme = self._theme(q)
            if path in ("/", "/index.html"):
                return self._send_html(theme, "Dashboard", theme.render_index(app))
            if path == "/api/stats":
                return self._json(app.stats)
            if path == "/api/emails":
                return self._json(app.filtered(q))
            if path == "/api/score":
                return self._json(app.score or {"error": "no ground truth found"})
            if path == "/score":
                if not app.score:
                    return self._err(404, "no ground truth file; pass --gt ground_truth.json")
                return self._send_html(theme, "Score", theme.render_score(app))
            if path == "/export":
                return self._export()
            m = re.fullmatch(r"/email/(\S+)", path)
            if m and m.group(1) in app.details:
                return self._send_html(theme, m.group(1), theme.render_email(app, m.group(1)))
            m = re.fullmatch(r"/api/email/(\S+)", path)
            if m and m.group(1) in app.details:
                return self._json(app.detail(m.group(1)))
            m = re.fullmatch(r"/api/hitl/explain/(\S+)", path)
            if m and m.group(1) in app.details:
                return self._json(app.explain(m.group(1), (q.get("q") or [""])[0] or None))
            m = re.fullmatch(r"/api/review/(\S+)", path)
            if m and m.group(1) in app.details:
                return self._json(app.store.get(m.group(1)) or {"email_id": m.group(1), "resolved": False})
            m = re.fullmatch(r"/api/draft/(\S+)", path)
            if m and m.group(1) in app.details:
                return self._json(app.draft(m.group(1)))
            if path == "/api/hitl/status":
                return self._json({"backend": app.store.backend, "config": {
                    "supabase": bool(app.store.cfg.get("configured")),
                    "gemini": bool(os.environ.get("GEMINI_API_KEY")),
                }})
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self._err(404, "not found")
        except Exception as exc:  # noqa: BLE001
            self._err(500, f"internal error: {exc}")

    def do_POST(self):  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/review":
                length = int(self.headers.get("Content-Length", "0") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                eid = payload.get("email_id", "")
                if eid not in app.details:
                    return self._json({"error": "unknown email_id"}, 404)
                if payload.get("resolved_category") not in CATEGORIES:
                    return self._json({"error": "bad resolved_category"}, 400)
                if payload.get("resolved_status") not in STATUSES:
                    return self._json({"error": "bad resolved_status"}, 400)
                saved = app.resolve(eid, payload)
                return self._json({"ok": True, **saved})
            if parsed.path == "/api/revert":
                length = int(self.headers.get("Content-Length", "0") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                eid = payload.get("email_id", "")
                if eid not in app.details:
                    return self._json({"error": "unknown email_id"}, 404)
                saved = app.revert(eid)
                return self._json({"ok": True, **saved})
            if parsed.path == "/api/revert-all":
                saved = app.reset_all()
                return self._json({"ok": True, **saved})
            if parsed.path == "/api/draft/approve":
                length = int(self.headers.get("Content-Length", "0") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                eid = payload.get("email_id", "")
                if eid not in app.details:
                    return self._json({"error": "unknown email_id"}, 404)
                saved = app.approve_draft(payload)
                return self._json({"ok": True, **saved})
            return self._err(404, "not found")
        except Exception as exc:  # noqa: BLE001
            self._err(500, f"internal error: {exc}")

    def _send_html(self, theme: Theme, title: str, body: str):
        self._send(200, self._page(theme, title, body), "text/html; charset=utf-8")

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Web UI for the SDOC pipeline")
    parser.add_argument("--data-dir", default=".")
    parser.add_argument("--gt", default="ground_truth.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--theme", choices=sorted(themes), default="modern",
                        help="default UI theme (modern or classic)")
    parser.add_argument("--dev", action="store_true",
                        help="show the classic/modern theme toggle in the top bar (development only)")
    args = parser.parse_args()

    global app, default_theme, dev_mode
    default_theme = args.theme
    dev_mode = args.dev
    print(f"building pipeline over {args.data_dir} ...")
    app = App(args.data_dir, args.gt)
    print(f"HITL backend: {app.store.backend} "
          f"(supabase={'on' if app.store.cfg.get('configured') else 'off'}, "
          f"gemini={'on' if os.environ.get('GEMINI_API_KEY') else 'off'})")
    if app.score:
        s = app.score
        print(f"local score: final={s['final_score']} stage1_macro_f1={s['stage1']['macro_f1']} "
              f"defect_f1={s['stage3']['defect_f1']} end_to_end={s['end_to_end']['rate']}")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"SDOC web UI [{args.theme}]: http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
