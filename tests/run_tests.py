"""End-to-end verification of the SDOC pipeline.

Runs the full pipeline over the local data bundle (copied from the organizer's
distro) and asserts the submission matches ground truth exactly.

Run from the project root:

    python tests/run_tests.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loader import Inbox  # noqa: E402
from main import build_submission  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT
GT_PATH = PROJECT / "ground_truth.json"


def main() -> int:
    if not (DATA_DIR / "inbox").exists() or not GT_PATH.exists():
        print("SKIP: local data bundle not present (run once: copy bundle into ./inbox "
              "and ./attachments, plus './ground_truth.json')")
        return 0

    inbox = Inbox(str(DATA_DIR))
    submission = build_submission(inbox)

    with open(GT_PATH, encoding="utf-8") as f:
        ground_truth = json.load(f)

    assert len(submission) == len(ground_truth), (
        f"expected {len(ground_truth)} entries, got {len(submission)}"
    )

    mismatches = [eid for eid in ground_truth if submission.get(eid) != ground_truth[eid]]
    if mismatches:
        print(f"FAIL: {len(mismatches)} emails differ from ground truth")
        for eid in mismatches[:10]:
            print(f"  {eid}: got {submission[eid]!r} want {ground_truth[eid]!r}")
        return 1

    print(f"OK: {len(ground_truth)}/{len(ground_truth)} entries match ground truth exactly")
    return 0


if __name__ == "__main__":
    sys.exit(main())