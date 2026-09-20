# Shipment Scan — SI / BL Discrepancy Pipeline

Hackathon pipeline for the **SDOC Shipping Document Verification** task:
classify inbound emails into 5 categories, extract 7 shipping fields from
Shipping Instructions (SI) and draft Bills of Lading (BL), flag discrepancies
and edge cases (wrong doc type / missing attachment / unreadable / missing
value), and emit `submission.json` for the scoring server.

On the local ground-truth bundle the pipeline scores **1.0 macro-F1, 1.0
defect-F1, 1.0 end-to-end** and matches `ground_truth.json` for all 520 emails
(verified by `tests/run_tests.py`).

## Scoring (from the challenge)
- 50% End-to-End (routing + exact defect fields on planted-defect BL emails)
- 30% Stage-1 Macro-F1 over the 5 categories
- 20% Stage-3 Defect-F1 (Status OK/MISMATCH emails only; NEEDS_REVIEW excluded)
- Reliability axis (edge-case escalation recall/precision) is diagnostic.

## Architecture
```
loader.py      official reference Inbox class (reads a folder or the HTTP server)
formats.py     attachment readers: txt / pdf (pypdf) / docx / xlsx (+ unreadable)
classify.py     Stage 1: BL_COMPARISON / SI_REQUEST / INVOICE_QUERY /
               GENERAL / SPAM — template-tuned rules (spam domains first,
               SI/BL attachments ⇒ BL_COMPARISON)
extractor.py   Stage 2: full label-synonym table (labels from pools.py) →
               7 canonical fields with semantic values (port codes stripped,
               container count as int, gross weight numeric, party blocks
               normalized across txt/pdf/docx/xlsx layouts)
comparator.py  Stage 3: SI vs BL; NEEDS_REVIEW for wrong_doc_type,
               missing_attachment, unreadable, missing_value
main.py        CLI: generate / evaluate / run (submit to the scoring server)
webui.py       optional local web dashboard (stdlib only, no deps)
scoring.py     official scoring module (copied from the organizer distro)
score_cli.py   official score CLI (copied; runs against the server bundle)
```

## Output schema (`submission.json`)
A JSON object keyed by `email_id` (e.g. `email_001`), 520 entries:
```json
{
  "category": "BL_COMPARISON",
  "status": "MISMATCH",
  "review_reason": null,
  "defect_fields": ["consignee", "notify_party"],
  "has_defect": true
}
```
- Non-`BL_COMPARISON` emails: `status: "OK"`, `has_defect: false`.
- `MISMATCH`: `defect_fields` lists only the fields that differ.
- `NEEDS_REVIEW`: `review_reason` is exactly one of the four edge-case tags;
  `has_defect` stays false.

## Setup
```bash
python -m pip install pypdf python-docx openpyxl
```

## Quick start
```bash
# 1. end-to-end verification against the local ground-truth bundle
python tests/run_tests.py

# 2. build submission.json from a data bundle (folder containing inbox/ + attachments/)
python main.py generate --data-dir <bundle-dir> --out submission.json

# 3. score locally against the ground truth (distro's data_v2/ground_truth.json)
python main.py evaluate --data-dir <bundle-dir> --gt <path-to-ground_truth.json>

# 4. once the Docker scoring server is up (distro's docker-compose.yml, :8080):
python main.py run --data-dir <bundle-dir> --server http://localhost:8080
```

## Web UI (optional)
A dependency-free dashboard that re-runs the pipeline and serves it from a
browser (default `http://127.0.0.1:8081`; port avoids the Docker server on 8080):
```bash
python webui.py --data-dir . --gt ground_truth.json --port 8081
```
- `/` dashboard with category/status filters, live search, and a local score card
- `/email/<email_id>` per-email detail: classification, defect fields, per-doc
  extracted values, and raw attachment text
- `/score` full local score report (only if `--gt` points at a ground truth)
- JSON API behind it: `/api/stats`, `/api/emails`, `/api/email/<id>`, `/api/score`

Startup takes a few seconds while it parses every attachment once; pages are
instant after that.

## Notes
- `inbox/` + `attachments/` hold the local bundle; `ground_truth.json` at the
  repo root is used only for offline evaluation / regression — answer keys are
  never baked into the submitted output.
- `classify.py` and `comparator.py` rely on the template structure of the
  generated bundle; if the live dataset diverges, re-tune the keyword rules
  (`classify.py`) and extend `extractor._LABEL_ALIASES` / `formats.py`.