"""NEW-234: plan-review helpers shared by the planning flows.

Moved from activities/ui_plan.py (NEW-123, NEW-139, NEW-142): fenced and bounded prompt
sections, and the cumulative findings a redraft or a re-review carries (deduplicated,
labelled by round, bounded) with their prompt block. The UI migration plan and the backlog
decomposition use the same review-convergence rules.

The bounds, the finding formatter and the fence are explicit keyword arguments (resolved
at call time when omitted): ui_plan keeps thin wrappers that pass its own module globals,
so rebinding ui_plan.PRIOR_FINDINGS_MAX, ui_plan._fenced and the like behaves as before.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

__all__ = [
    "PRIOR_FINDINGS_CHARS",
    "PRIOR_FINDINGS_MAX",
    "bounded_json",
    "cumulative_findings",
    "fenced",
    "finding_text",
    "findings_block",
]


def fenced(label: str, content: str) -> str:
    content = content.replace("--- BEGIN", "[fence removed]").replace("--- END", "[fence removed]")
    return f"--- BEGIN {label.upper()} ---\n{content.strip()}\n--- END {label.upper()} ---"


def bounded_json(value: Any, limit: int) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + " [truncated]"


# NEW-139: review convergence. Every review of a part is exhaustive, and every redraft and
# re-review carries all earlier findings on that part (labelled by round), bounded.
PRIOR_FINDINGS_MAX = 60
PRIOR_FINDINGS_CHARS = 40_000


def finding_text(item: Any) -> str:
    if isinstance(item, dict):
        text = (
            f"[{item.get('category') or '-'}] {item.get('subject') or '-'}: "
            f"{item.get('detail') or ''}"
        )
    else:
        text = str(item)
    return " ".join(text.split())[:2_000]


def cumulative_findings(
    reviews: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
    before: int,
    previous: dict[str, Any] | None = None,
    *,
    bounded: bool = True,
    max_findings: int | None = None,
    max_chars: int | None = None,
    format_finding: Callable[[Any], str] | None = None,
) -> tuple[list[str], int]:
    """Every REVISE finding and Hermes rejection finding on drafts 1..before-1 of a part,
    deduplicated (a repeated finding lists every round it was raised in) and labelled by
    round, ordered by each finding's latest occurrence. Past PRIOR_FINDINGS_MAX findings or
    PRIOR_FINDINGS_CHARS characters the findings last raised longest ago are omitted (the
    newest rounds, repeats included, are kept whole); returns the lines and the number
    omitted.

    NEW-142: `previous` (blueprint_id, reviews, rejections) is the earlier plan run this
    part continues; all its findings come first, labelled "previous run <id>, round N".

    NEW-234: `max_findings`, `max_chars` and `format_finding` default to PRIOR_FINDINGS_MAX,
    PRIOR_FINDINGS_CHARS and finding_text."""
    if max_findings is None:
        max_findings = PRIOR_FINDINGS_MAX
    if max_chars is None:
        max_chars = PRIOR_FINDINGS_CHARS
    text_of = finding_text if format_finding is None else format_finding
    raised: list[tuple[tuple[int, int], str, str]] = []

    def collect(
        run: int, prefix: str, items: list[dict[str, Any]], fails: list[dict[str, Any]], limit: int
    ) -> None:
        for review in items:
            attempt = int(review.get("attempt") or 0)
            if 1 <= attempt < limit and review.get("decision") == "REVISE":
                label = f"{prefix}round {attempt} (review of draft {attempt})"
                raised.extend(
                    ((run, attempt), label, text_of(item)) for item in review.get("findings") or []
                )
        for rejection in fails:
            attempt = int(rejection.get("attempt") or 0)
            if 1 <= attempt < limit:
                source = f"Hermes rejection {rejection.get('code') or ''}".strip()
                label = f"{prefix}round {attempt} ({source} of draft {attempt})"
                raised.extend(
                    ((run, attempt), label, text_of(item))
                    for item in rejection.get("details") or []
                )

    if previous:
        collect(
            0,
            f"previous run {str(previous.get('blueprint_id'))[:8]}, ",
            list(previous.get("reviews") or []),
            list(previous.get("rejections") or []),
            1_000,
        )
    collect(1, "", reviews, rejections, before)
    rounds: dict[str, list[str]] = {}
    latest: dict[str, int] = {}
    for position, (_order, label, text) in enumerate(sorted(raised, key=lambda x: x[0])):
        if label not in rounds.setdefault(text, []):
            rounds[text].append(label)
        latest[text] = position
    # NEW-139 review: order by each finding's latest occurrence, so a finding repeated in
    # the newest round (still unresolved) is kept before older round-only findings.
    order = sorted(rounds, key=lambda text: latest[text])
    lines = [f"- [{'; '.join(rounds[text])}] {text}" for text in order]
    if not bounded:
        # NEW-142 review: the complete list, staged with a continued draft.
        return lines, 0
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        if len(kept) >= max_findings or size + len(line) + 1 > max_chars:
            break
        kept.append(line)
        size += len(line) + 1
    kept.reverse()
    return kept, len(lines) - len(kept)


def findings_block(
    label: str,
    lines: list[str],
    omitted: int,
    previous_file: str | None = None,
    *,
    max_findings: int | None = None,
    max_chars: int | None = None,
    fence: Callable[[str, str], str] | None = None,
) -> str:
    """NEW-142 review: when a previous run's findings are part of the list, the complete
    list of them is in the staged continuation file (`previous_file`), field
    prior_findings; this run's own are in its review files.

    NEW-234: the bounds the note names and the fence default to PRIOR_FINDINGS_MAX,
    PRIOR_FINDINGS_CHARS and fenced."""
    if max_findings is None:
        max_findings = PRIOR_FINDINGS_MAX
    if max_chars is None:
        max_chars = PRIOR_FINDINGS_CHARS
    where = (
        f"this run's are in the earlier review files of this part in the input directory, "
        f"and the complete list from the previous run is in {previous_file} (field "
        "prior_findings)"
        if previous_file
        else "they are in the earlier review files of this part in the input directory"
    )
    note = (
        f"\n({omitted} older findings are omitted past the bound of {max_findings} "
        f"findings or {max_chars} characters; {where}, and they still apply.)"
        if omitted
        else ""
    )
    return (fenced if fence is None else fence)(label, "\n".join(lines) + note)
