"""Build the Vercel snapshot: precomputed pipeline output committed to the repo.

Run locally whenever the pipeline or data changes:

    python scripts/build_snapshot.py --data-dir . --gt ground_truth.json

Writes api/_snapshot/snapshot.json with BASE pipeline rows (no HITL overrides),
per-email details, aggregate score, and build metadata. Vercel functions serve
this file directly — no attachment parsing at request time. ground_truth.json
itself is never committed or shipped.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import webui  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Vercel snapshot")
    parser.add_argument("--data-dir", default=".")
    parser.add_argument("--gt", default="ground_truth.json")
    parser.add_argument("--out", default="api/_snapshot/snapshot.json")
    args = parser.parse_args()

    _, rows, details = webui._build(args.data_dir)
    sub = {
        r["email_id"]: {
            k: r[k]
            for k in ("category", "status", "review_reason", "defect_fields", "has_defect")
        }
        for r in rows
    }
    score = webui._score_report(sub, args.gt)

    snapshot = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_dir": args.data_dir,
            "count": len(rows),
            "has_score": score is not None,
        },
        "rows": rows,
        "details": details,
        "sub": sub,
        "score": score,
    }
    out = PROJECT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    rows_out = copy.deepcopy(rows)
    print(f"snapshot: {len(rows)} rows -> {args.out} "
          f"({out.stat().st_size / 1024:.0f} KB, score={'yes' if score else 'no'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
