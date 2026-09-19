#!/usr/bin/env python3
"""
CLI leaderboard for the SDOC hackathon (JUDGES) — v2 dataset.

    python score_cli.py submission.json
    python score_cli.py submission.json --ground-truth ../data_v2/ground_truth.json
    python score_cli.py submission.json --json      # machine-readable

Grades against a ground_truth.json the judges hold privately. Participants
never have this file; they submit predictions (see sample_submission.json for
the exact shape).
"""
import argparse
import json
from pathlib import Path

import scoring

HERE = Path(__file__).parent
DEFAULT_GT = HERE.parent / "data_v2" / "ground_truth.json"


def bar(x, width=24):
    n = int(round(x * width))
    return "█" * n + "·" * (width - n)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("submission")
    ap.add_argument("--ground-truth", default=str(DEFAULT_GT))
    ap.add_argument("--weights", help="optional JSON overriding stage weights")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON only")
    args = ap.parse_args()

    truth = json.loads(Path(args.ground_truth).read_text())
    sub = json.loads(Path(args.submission).read_text())
    weights = json.loads(Path(args.weights).read_text()) if args.weights else None

    r = scoring.score_all(truth, sub, weights)

    if args.json:
        print(json.dumps(r, indent=2))
        return

    s1, s3, rel, e2e = r["stage1"], r["stage3"], r["reliability"], r["end_to_end"]
    print("=" * 62)
    print(f"  SDOC HACKATHON SCORE  —  {Path(args.submission).name}")
    print(f"  {r['n_emails']} emails")
    print("=" * 62)

    print("\nSTAGE 1 · Email classification")
    print(f"  accuracy      {s1['accuracy']:.3f}  {bar(s1['accuracy'])}")
    print(f"  macro-F1      {s1['macro_f1']:.3f}  {bar(s1['macro_f1'])}")
    if s1["rule_pct"] is not None:
        print(f"  resolved by rules (cost): {s1['rule_pct']:.0%}")
    print("\n  per-category  precision / recall / f1")
    for c in scoring.CATEGORIES:
        p, rr, f = scoring.prf(**s1["per"][c])
        print(f"    {c:<15} {p:.2f} / {rr:.2f} / {f:.2f}")

    print("\nSTAGE 3 · BL-vs-SI comparison  (comparable doc emails)")
    print(f"  defect recall     {s3['defect_recall']:.3f}  {bar(s3['defect_recall'])}")
    print(f"  defect precision  {s3['defect_precision']:.3f}  {bar(s3['defect_precision'])}")
    print(f"  field-level F1    {s3['field_f1']:.3f}  {bar(s3['field_f1'])}")
    print(f"  exact-match rate  {s3['exact_match_rate']:.3f}")

    print("\nRELIABILITY · escalate what you can't decide  (diagnostic)")
    print(f"  escalation recall     {rel['escalation_recall']:.3f}  {bar(rel['escalation_recall'])}")
    print(f"  escalation precision  {rel['escalation_precision']:.3f}  {bar(rel['escalation_precision'])}")
    print(f"  gold NEEDS_REVIEW: {rel['gold_review']}   flagged: {rel['pred_review']}")
    for rn, d in rel["per_reason"].items():
        print(f"    {rn:<20} {d['caught']}/{d['total']} escalated")

    print("\nEND-TO-END · the headline metric")
    print(f"  {e2e['success']}/{e2e['total']} defect emails caught end to end")
    print(f"  rate  {e2e['rate']:.3f}  {bar(e2e['rate'])}")

    w = r["weights"]
    print("\n" + "-" * 62)
    print(f"  FINAL SCORE  {r['final_score']:.4f}   "
          f"(w: s1={w['stage1']}, s3={w['stage3']}, e2e={w['end_to_end']})")
    print("-" * 62)


if __name__ == "__main__":
    main()
