#!/usr/bin/env python3
"""
SDOC hackathon scoring — v2 dataset (data_v2/).

Grades a submission against ground_truth.json. This version understands the
richer v2 schema, where every record carries:

    category      : BL_COMPARISON | SI_REQUEST | INVOICE_QUERY | GENERAL | SPAM
    status        : OK | MISMATCH | NEEDS_REVIEW
    review_reason : null | wrong_doc_type | missing_attachment | unreadable | missing_value
    has_defect    : bool          (true iff status == MISMATCH)
    defect_fields : [str]         (the mismatched fields, when MISMATCH)

Headline metric is unchanged from the original kit (end-to-end defect catch),
so leaderboards stay comparable. NEEDS_REVIEW is scored as a separate
diagnostic "reliability" axis: did the pipeline correctly escalate the cases
it cannot decide, instead of false-alarming them as clean or as defects?

This module is imported by both the CLI (score_cli.py) and the server (app.py).
"""
from collections import defaultdict

CATEGORIES = ["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]
REVIEW_REASONS = ["wrong_doc_type", "missing_attachment", "unreadable", "missing_value"]

# End-to-end is what wins; the stage scores are diagnostic. Matches the
# original scoring/score.py weights so the leaderboard is comparable.
DEFAULT_WEIGHTS = {"stage1": 0.30, "stage3": 0.20, "end_to_end": 0.50}


def prf(tp, fp, fn):
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def score_stage1(truth, sub):
    """Classification accuracy + per-category P/R/F1 + confusion matrix."""
    per = {c: {"tp": 0, "fp": 0, "fn": 0} for c in CATEGORIES}
    confusion = defaultdict(lambda: defaultdict(int))  # actual -> predicted -> n
    correct = 0
    rule_hits = rule_total = 0
    for eid, t in truth.items():
        actual = t["category"]
        pred = sub.get(eid, {}).get("category", "GENERAL")  # missing => default
        confusion[actual][pred] += 1
        if pred == actual:
            correct += 1
            per[actual]["tp"] += 1
        else:
            per[actual]["fn"] += 1
            if pred in per:
                per[pred]["fp"] += 1
        decided_by = sub.get(eid, {}).get("decided_by")
        if decided_by is not None:
            rule_total += 1
            if decided_by == "rule":
                rule_hits += 1
    accuracy = correct / len(truth) if truth else 0.0
    macro_f1 = sum(prf(**per[c])[2] for c in CATEGORIES) / len(CATEGORIES)
    rule_pct = (rule_hits / rule_total) if rule_total else None
    return {"accuracy": accuracy, "macro_f1": macro_f1, "per": per,
            "confusion": confusion, "rule_pct": rule_pct}


def score_stage3(truth, sub):
    """Defect detection on doc emails that were actually comparable (status
    OK or MISMATCH). NEEDS_REVIEW cases are excluded here — they are graded on
    the reliability axis, not as match/mismatch."""
    tp = fp = fn = 0                       # email-level: did we catch a defect?
    f_tp = f_fp = f_fn = 0                 # field-level: which fields?
    exact = doc_total = 0
    for eid, t in truth.items():
        if t["category"] != "BL_COMPARISON":
            continue
        if t.get("status") == "NEEDS_REVIEW":
            continue
        doc_total += 1
        s = sub.get(eid, {})
        routed = s.get("category") == "BL_COMPARISON"
        pred_defect = bool(s.get("has_defect")) and routed
        pred_fields = set(s.get("defect_fields", [])) if routed else set()
        gold_defect = t["has_defect"]
        gold_fields = set(t["defect_fields"])

        if gold_defect and pred_defect:
            tp += 1
        elif gold_defect and not pred_defect:
            fn += 1
        elif not gold_defect and pred_defect:
            fp += 1

        if pred_fields == gold_fields:
            exact += 1
        f_tp += len(pred_fields & gold_fields)
        f_fp += len(pred_fields - gold_fields)
        f_fn += len(gold_fields - pred_fields)

    p, r, f = prf(tp, fp, fn)
    _, _, ff_ = prf(f_tp, f_fp, f_fn)
    return {"defect_precision": p, "defect_recall": r, "defect_f1": f,
            "field_f1": ff_, "exact_match_rate": exact / doc_total if doc_total else 0.0,
            "doc_total": doc_total}


def score_reliability(truth, sub):
    """Reliability / human-review axis (diagnostic).

    For the NEEDS_REVIEW cases (unreadable, wrong doc, missing attachment,
    missing value) the correct behaviour is to ESCALATE, not to report a clean
    pass or invent a defect. We credit a submission that marks status
    NEEDS_REVIEW (routed as BL_COMPARISON). We also report escalation
    precision: of everything the team escalated, how much genuinely needed it.
    """
    esc_tp = esc_fn = 0                     # of gold NEEDS_REVIEW, how many escalated
    per_reason = {rn: {"total": 0, "caught": 0} for rn in REVIEW_REASONS}
    gold_review = pred_review = 0
    esc_correct = 0
    for eid, t in truth.items():
        s = sub.get(eid, {})
        pred_needs = s.get("status") == "NEEDS_REVIEW"
        gold_needs = t.get("status") == "NEEDS_REVIEW"
        if pred_needs:
            pred_review += 1
            if gold_needs:
                esc_correct += 1
        if gold_needs:
            gold_review += 1
            rn = t.get("review_reason")
            if rn in per_reason:
                per_reason[rn]["total"] += 1
                if pred_needs:
                    per_reason[rn]["caught"] += 1
            if pred_needs:
                esc_tp += 1
            else:
                esc_fn += 1
    recall = esc_tp / gold_review if gold_review else 0.0
    precision = esc_correct / pred_review if pred_review else 0.0
    f = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"escalation_recall": recall, "escalation_precision": precision,
            "escalation_f1": f, "gold_review": gold_review,
            "pred_review": pred_review, "per_reason": per_reason}


def score_end_to_end(truth, sub):
    """The headline metric. A doc email with a planted defect only 'succeeds'
    if the pipeline (a) routed it to BL_COMPARISON AND (b) flagged the EXACT
    defect fields. Unchanged from the original kit."""
    total = success = 0
    for eid, t in truth.items():
        if not (t["category"] == "BL_COMPARISON" and t.get("has_defect")):
            continue
        total += 1
        s = sub.get(eid, {})
        routed = s.get("category") == "BL_COMPARISON"
        flagged = bool(s.get("has_defect"))
        fields_ok = set(s.get("defect_fields", [])) == set(t["defect_fields"])
        if routed and flagged and fields_ok:
            success += 1
    return {"success": success, "total": total,
            "rate": success / total if total else 0.0}


def score_all(truth, sub, weights=None):
    """Run every axis and compute the final weighted score. Returns a plain
    dict (JSON-serialisable except the confusion defaultdicts, which callers
    can convert). This is the single entry point used by the CLI and server."""
    weights = weights or DEFAULT_WEIGHTS
    s1 = score_stage1(truth, sub)
    s3 = score_stage3(truth, sub)
    rel = score_reliability(truth, sub)
    e2e = score_end_to_end(truth, sub)
    final = (weights["stage1"] * s1["macro_f1"]
             + weights["stage3"] * s3["defect_f1"]
             + weights["end_to_end"] * e2e["rate"])
    return {
        "stage1": {"accuracy": s1["accuracy"], "macro_f1": s1["macro_f1"],
                   "rule_pct": s1["rule_pct"], "per": s1["per"],
                   "confusion": {a: dict(d) for a, d in s1["confusion"].items()}},
        "stage3": s3,
        "reliability": rel,
        "end_to_end": e2e,
        "weights": weights,
        "final_score": final,
        "n_emails": len(truth),
    }
