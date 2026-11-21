"""One-hop, cited prerequisites for the Find related records Flow task."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal, select
from sqlalchemy.orm import Session

from engineering_control.dashboard.workspace_issues import STORY_REF
from engineering_control.db.models import ReviewEvidenceJob

Row = dict[str, Any]
HEADER = re.compile(
    r"^(?:#{1,6}\s+)?(?:depends on|blocked by|prerequisites?|dependencies|requires)\b"
    r"\s*:?\s*(.*)$",
    re.I,
)
SEPARATOR = re.compile(r"^(?:\s*(?:,|;|&|\band\b)\s*)+", re.I)
BULLET = re.compile(r"^\s*[-*+]\s+")
BRIDGE_LIMIT = 2


def declarations(record: Row) -> dict[str, str]:
    """Read forward dependency lists. Ordinary mentions and 'blocks' point elsewhere."""
    found: dict[str, str] = {}
    lines = record["body"].splitlines()
    listing = False
    fence = ""
    for n, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            marker = line[:3]
            if not fence or marker == fence:
                fence = "" if fence else marker
            listing = False
            continue
        if fence or not line:
            continue
        text = BULLET.sub("", line).replace("**", "").replace("`", "")
        header = HEADER.fullmatch(text)
        if header:
            text = header[1]
            listing = not text
        elif not listing:
            continue
        if not text:
            continue
        ref = STORY_REF.match(text)
        if not ref:
            listing = False
            continue
        while ref:
            repo = ref[1] or ref[2] or record["repository"]
            key = f"{repo.casefold()}#{ref[3]}"
            # A nearby sentence often explains the input named in the list. Include only a
            # neighboring line that cites that same issue, never the rest of the document.
            explanation = next(
                (
                    other.strip()[:360]
                    for other in lines[n + 1 : n + 4]
                    if not HEADER.match(BULLET.sub("", other.strip()))
                    and any(
                        f"{(r[1] or r[2] or record['repository']).casefold()}#{r[3]}" == key
                        for r in STORY_REF.finditer(other)
                    )
                ),
                "",
            )
            found.setdefault(
                key, f"L{n + 1} {line[:360]}" + ("\n" + explanation if explanation else "")
            )
            sep = SEPARATOR.match(text[ref.end() :])
            if not sep:
                break
            text = text[ref.end() + sep.end() :]
            ref = STORY_REF.match(text)
    return found


def reviewed_scopes(db: Session, pid: UUID, records: dict[str, Row]) -> dict[str, dict[str, str]]:
    """Read current-revision verdicts. A human judgment takes priority over a model receipt."""
    job = ReviewEvidenceJob
    sample = job.result["sample"]
    issue, story = (sample[k].as_string() for k in ("issue", "story"))
    human = job.result["human_review"]
    advisory = job.result["advisory"]
    # Project just two scalars from the frozen graph, which can be several megabytes.
    if db.get_bind().dialect.name == "postgresql":
        revisions = [
            func.json_extract_path_text(job.source, "records", k, "revision")
            for k in (issue, story)
        ]
    else:
        revisions = [
            func.json_extract(job.source, literal('$.records."') + k + literal('".revision'))
            for k in (issue, story)
        ]
    rows = db.execute(
        select(issue, story, *revisions, human["scope"].as_string(), advisory["scope"].as_string())
        .where(
            job.project_id == pid,
            (human["scope"].as_string().is_not(None))
            | ((job.state == "complete") & (advisory["status"].as_string() == "advisory")),
            job.result["repeat_of"].as_string().is_(None),
        )
        .order_by(
            human["at"].as_string().desc().nulls_last(),
            job.claimed_at.desc().nulls_last(),
            job.run_id.desc(),
        )
    )
    result: dict[str, dict[str, str]] = defaultdict(dict)
    for i, s, ir, sr, decided, suggested in rows:
        if (
            i in records
            and s in records
            and records[i]["revision"] == ir
            and records[s]["revision"] == sr
        ):
            result[i].setdefault(s, decided or suggested)
    return dict(result)


def bridges(src: Row) -> dict[str, dict[str, list[Row]]]:
    records: dict[str, Row] = src["records"]
    numbered = {
        f"{r['repository'].casefold()}#{r['number']}": k
        for k, r in records.items()
        if r.get("number") and r["kind"] == "issues" and r.get("state_reason") != "not_planned"
    }
    stories = {
        f"{r['repository'].casefold()}#{r['number']}": k
        for k, r in records.items()
        if r.get("number") and r["kind"] == "stories"
    }
    found: dict[str, dict[str, list[Row]]] = defaultdict(lambda: defaultdict(list))
    for key, row in sorted(records.items()):
        if row["kind"] == "stories":
            anchors = {key: "Story declaration"}
        elif row["kind"] == "issues" and row.get("state_reason") != "not_planned":
            anchors = {
                stories[ref]: "declared Story" for ref in row.get("refs", ()) if ref in stories
            }
            if row.get("manual"):
                anchors.update(dict.fromkeys(row["parents"], "manual Story link"))
            for s, scope in src.get("reviewed_scopes", {}).get(key, {}).items():
                if scope == "full":
                    anchors[s] = "full coverage review"
                else:
                    anchors.pop(s, None)
        else:
            continue
        if not anchors:
            continue
        for ref, passage in declarations(row).items():
            upstream = numbered.get(ref)
            if not upstream or upstream == key:
                continue
            for story, basis in sorted(anchors.items()):
                if story not in records or src["decisions"].get(key, {}).get(story) == "dismissed":
                    continue
                if (
                    row.get("manual")
                    and records[story]["project"] == row["project"]
                    and story not in row["parents"]
                ):
                    continue
                # The review inbox checks access to both endpoints. Do not introduce a third
                # project's source into either the provider input or the stored verdict.
                if row["project"] not in (records[upstream]["project"], records[story]["project"]):
                    continue
                evidence = found[upstream][story]
                if len(evidence) < BRIDGE_LIMIT:
                    evidence.append(
                        {
                            "source": f"{row['repository']}#{row['number'] or row['local']}",
                            "title": row["title"],
                            "revision": row["revision"][:16],
                            "passage": passage,
                            "basis": basis,
                        }
                    )
    return found
