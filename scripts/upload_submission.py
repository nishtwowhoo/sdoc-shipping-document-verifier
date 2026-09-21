"""Upload submission.json to Supabase (stdlib only, no new pip deps).

Run once in the Supabase SQL editor to create the table:

    create table if not exists submissions (
      email_id text primary key,
      category text,
      status text,
      has_defect boolean,
      review_reason text,
      defect_fields jsonb default '[]',
      run_id text default '',
      updated_at timestamptz default now()
    );

Usage:
    python scripts/upload_submission.py --file submission.json
    python scripts/upload_submission.py --file submission.json --dry-run
    python scripts/upload_submission.py --file submission.json --table submissions --run-id manual-01

Config reuses hitl.supabase_cfg(): SUPABASE_URL plus SUPABASE_KEY (or
SUPABASE_ANON_KEY / SUPABASE_SERVICE_KEY), read from env or .env.
Rows are upserted on email_id in batches, so re-running is safe.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import hitl  # noqa: E402


def table_sql(table: str) -> str:
    return f"""create table if not exists {table} (
  email_id text primary key,
  category text,
  status text,
  has_defect boolean,
  review_reason text,
  defect_fields jsonb default '[]',
  run_id text default '',
  updated_at timestamptz default now()
);"""


def to_record(email_id: str, entry: dict, run_id: str) -> dict:
    entry = entry if isinstance(entry, dict) else {}
    return {
        "email_id": email_id,
        "category": entry.get("category"),
        "status": entry.get("status"),
        "has_defect": bool(entry.get("has_defect")),
        "review_reason": entry.get("review_reason"),
        "defect_fields": entry.get("defect_fields") or [],
        "run_id": run_id,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def table_exists(cfg: dict) -> bool:
    url = f"{cfg['url']}/rest/v1/{cfg['table']}?select=email_id&limit=1"
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
    }
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        return True
    except Exception:
        return False


def upsert_batch(cfg: dict, records: list) -> None:
    url = f"{cfg['url']}/rest/v1/{cfg['table']}"
    payload = json.dumps(records).encode("utf-8")
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload submission.json to Supabase")
    parser.add_argument("--file", default="submission.json")
    parser.add_argument("--table", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = hitl.supabase_cfg()
    # Submissions get their own table (never the HITL reviews table).
    cfg["table"] = (args.table
                    or os.environ.get("SUPABASE_SUBMISSIONS_TABLE", "")
                    or "submissions")
    run_id = args.run_id or datetime.datetime.now(datetime.timezone.utc).strftime(
        "run-%Y%m%d-%H%M%S")

    path = PROJECT / args.file
    data = json.loads(path.read_text(encoding="utf-8"))
    records = [to_record(eid, entry, run_id) for eid, entry in data.items()]
    print(f"loaded {len(records)} entries from {args.file} (run_id={run_id})")

    if not cfg["configured"]:
        print("Supabase not configured (SUPABASE_URL / SUPABASE_KEY missing).")
        print("Create the table with:")
        print(table_sql(cfg["table"]))
        return 2
    if args.dry_run:
        print(f"dry-run: would upsert {len(records)} rows into '{cfg['table']}'.")
        print("Sample record: " + json.dumps(records[0])[:200])
        return 0

    if not table_exists(cfg):
        print(f"Table '{cfg['table']}' not found in Supabase.")
        print("Run this once in the Supabase SQL editor, then re-run:")
        print(table_sql(cfg["table"]))
        return 3

    total = 0
    for i in range(0, len(records), args.batch_size):
        batch = records[i:i + args.batch_size]
        try:
            upsert_batch(cfg, batch)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            print(f"FAILED batch {i // args.batch_size + 1}: HTTP {exc.code}: {detail}")
            print("If the table is missing, create it with:")
            print(table_sql(cfg["table"]))
            return 1
        total += len(batch)
        print(f"upserted {total}/{len(records)}")
    print(f"done: {total} rows in '{cfg['table']}' (run_id={run_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
