"""Build the submission.json for the SDOC Shipping Document Verification task.

Usage:
  python main.py generate --data-dir <dir with inbox/attachments> --out submission.json
  python main.py evaluate --data-dir <dir> --gt <ground_truth.json>
  python main.py run --data-dir <dir> --server http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any

from classify import classify
from comparator import Result, analyze_email
from loader import Inbox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402


def build_submission(inbox: Inbox) -> dict[str, dict[str, Any]]:
    sub: dict[str, dict[str, Any]] = {}
    for email in inbox.emails():
        classification = classify(email)
        result: Result = analyze_email(inbox, email, classification.category)
        sub[email["email_id"]] = result.as_dict()
    return sub


def _load(inbox_dir: str, ground_truth: str | None) -> tuple[dict, dict | None]:
    gt = None
    if ground_truth:
        with open(ground_truth, encoding="utf-8") as f:
            gt = json.load(f)
    inbox = Inbox(inbox_dir)
    return inbox, gt


def cmd_generate(args: argparse.Namespace) -> int:
    inbox, _ = _load(args.data_dir, None)
    sub = build_submission(inbox)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(sub, f, indent=2)
    print(f"wrote {len(sub)} entries -> {args.out}")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scoring

    inbox, gt = _load(args.data_dir, args.gt)
    sub = build_submission(inbox)
    report = json.loads(json.dumps(scoring.score_all(gt, sub), default=str))
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    inbox, _ = _load(args.data_dir, None)
    sub = build_submission(inbox)
    payload = json.dumps(sub).encode("utf-8")
    req = urllib.request.Request(
        args.server.rstrip("/") + "/submit",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310 - user-specified server
        print(resp.read().decode("utf-8"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SDOC submission builder")
    subs = parser.add_subparsers(dest="cmd", required=True)

    g = subs.add_parser("generate")
    g.add_argument("--data-dir", required=True, help="bundle dir containing inbox/")
    g.add_argument("--out", default="submission.json")

    e = subs.add_parser("evaluate")
    e.add_argument("--data-dir", required=True)
    e.add_argument("--gt", default="ground_truth.json")

    r = subs.add_parser("run")
    r.add_argument("--data-dir", required=True)
    r.add_argument("--server", default="http://localhost:8080")

    args = parser.parse_args()
    return {
        "generate": cmd_generate,
        "evaluate": cmd_evaluate,
        "run": cmd_run,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())