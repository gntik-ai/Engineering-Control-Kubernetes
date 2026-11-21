"""Score frozen Flow outputs against a separate administrator label file, offline.

Predictions JSON contains pairs (id, baseline, candidate), and two receipt lists
(baseline_receipts and candidate_receipts, each with id and eur). A scope is
full, partial, unrelated or unclear. Label JSON contains tuning_epics and labels
(id, epic, scope, optional explicit_link, orphan, cross_project). Only include
judgments recorded independently of either model's answer. Supply the same
source-revision pair IDs in both files. The script never dispatches a model.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

SCOPES = {"full", "partial", "unrelated", "unclear"}
LINK = {"full", "partial"}
Row = dict[str, Any]


def index(rows: list[Row]) -> dict[str, Row]:
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows) or not all(by_id):
        raise ValueError("Each pair or receipt must have a unique, nonempty ID.")
    return by_id


def spend(receipts: list[Row]) -> Decimal | None:
    values = [row.get("eur") for row in index(receipts).values()]
    if not values or None in values:
        return None
    amounts = [Decimal(str(v)) for v in values]
    if any(not n.is_finite() or n < 0 for n in amounts):
        raise ValueError("Invalid receipt amount.")
    return sum(amounts, Decimal(0))


def score(predictions: Row, reference: Row) -> Row:
    pairs = index(predictions["pairs"])
    labels = index(reference["labels"])
    if labels.keys() - pairs.keys():
        raise ValueError("Reference contains pairs absent from the frozen predictions.")
    if any(p.get(k) not in SCOPES for p in pairs.values() for k in ("candidate", "baseline")):
        raise ValueError("Unknown scope in predictions.")
    if any(lab.get("scope") not in SCOPES for lab in labels.values()):
        raise ValueError("Unknown scope in reference.")
    tuning = set(reference["tuning_epics"])
    if any(lab.get("epic") in tuning for lab in labels.values()):
        raise ValueError("A held-out Epic was also used for tuning.")
    judged = {k: lab for k, lab in labels.items() if lab["scope"] != "unclear"}
    metrics: Row = {}
    for arm in ("baseline", "candidate"):
        found: Counter[str] = Counter(
            {
                k: 0
                for k in (
                    "exact_scope",
                    "true_links",
                    "missed_links",
                    "false_links",
                    "abstentions",
                    "explicit_links_missed",
                )
            }
        )
        for key, lab in judged.items():
            expected, predicted = lab["scope"], pairs[key][arm]
            found["exact_scope"] += predicted == expected
            found["true_links"] += expected in LINK and predicted in LINK
            found["missed_links"] += expected in LINK and predicted not in LINK
            found["false_links"] += expected not in LINK and predicted in LINK
            found["abstentions"] += predicted == "unclear"
            found["explicit_links_missed"] += (
                bool(lab.get("explicit_link")) and predicted not in LINK
            )
        eur = spend(predictions[arm + "_receipts"])
        positives = found["true_links"] + found["missed_links"]
        metrics[arm] = {
            **found,
            "recall": found["true_links"] / positives if positives else None,
            "estimated_eur": float(eur) if eur is not None else None,
            "eur_per_independently_confirmed_link": float(eur / found["true_links"])
            if eur is not None and found["true_links"]
            else None,
        }
    old, new = (metrics[k]["estimated_eur"] for k in ("baseline", "candidate"))
    return {
        "pairs": len(pairs),
        "judged": len(judged),
        "unlabeled": len(pairs) - len(labels),
        "disagreements": sum(p["candidate"] != p["baseline"] for p in pairs.values()),
        "held_out_epics": len({lab["epic"] for lab in judged.values() if lab.get("epic")}),
        "orphan_labels": sum(bool(lab.get("orphan")) for lab in judged.values()),
        "cross_project_labels": sum(bool(lab.get("cross_project")) for lab in judged.values()),
        "explicit_links_tested": sum(bool(lab.get("explicit_link")) for lab in judged.values()),
        "metrics": metrics,
        "cost_reduction": 1 - new / old if old and new is not None else None,
        "rollout": "administrator_review",
    }


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("predictions", type=Path)
    cli.add_argument("reference", type=Path)
    args = cli.parse_args()
    print(
        json.dumps(
            score(json.loads(args.predictions.read_text()), json.loads(args.reference.read_text())),
            indent=2,
        )
    )
