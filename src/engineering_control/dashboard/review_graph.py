"""Read-only, project-scoped evidence tasks shared by the preview and review workers."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from engineering_control.dashboard import review_prerequisites
from engineering_control.dashboard.connection_records import catalog, counterpart, digest
from engineering_control.dashboard.issue_coverage import (
    EXCERPT_CHARACTERS,
    excerpt,
    inventory,
    technical_sources,
)
from engineering_control.dashboard.issue_types import bugs_with_optional_stories
from engineering_control.dashboard.workspace_issues import STORY_LINE, STORY_REF
from engineering_control.db.models import (
    ConnectionFinding,
    ProjectConnection,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectUserStory,
    ReviewWikiPage,
)
from engineering_control.engine import reviews

Row = dict[str, Any]
STOP = set(
    (
        "the and for are its you can that with this from when then has also will only into than "
        "have should what which project story user epic "
        "falcone serverless change request technical issue"
    ).split()
)
TERM = re.compile(r"[a-z][a-z0-9_]{2,}")
ID = re.compile(
    r"https?://github\.com/[\w./-]+/issues/\d+|(?:[\w.-]+/[\w.-]+)?#\d+|\b[A-Z]{2,8}-\d+\b", re.I
)
PARENT = re.compile(r"^\s*(?:[-*]\s*)?(?:parent|epic|(?:user )?stor(?:y|ies))\s*:.*$", re.I | re.M)
PRIORITY = re.compile(
    r"^\s*(?:[-*]\s*\[[ x]\]|as (?:a|an|the)\b)|\b(?:verifiable outcome|must|shall|"
    r"out of scope|depends on)\b",
    re.I,
)


def terms(row: Row) -> Counter[str]:
    text = row["title"] + "\n" + excerpt(row["body"])
    return Counter(
        t for t in TERM.findall(ID.sub(" ", PARENT.sub("", text)).lower()) if t not in STOP
    )


def source_key(project: UUID, kind: str, local: UUID) -> str:
    return f"{project}/{kind}/{local}"


def record(
    project: ProjectRecord,
    kind: str,
    local: UUID,
    number: int | None,
    title: str,
    body: str | None,
    state: str,
) -> Row:
    return {
        "project": str(project.id),
        "repository": project.source_repository,
        "kind": kind,
        "local": str(local),
        "number": number,
        "title": title,
        "body": body or "",
        "state": state,
        "revision": digest([title, body, state]),
        "parents": [],
    }


def snapshot(
    db: Session,
    project: ProjectRecord,
    peers: bool,
    provider: str,
    model: str,
    policy: str,
    *,
    reuse: bool = True,
) -> Row:
    links = (
        list(
            db.scalars(
                select(ProjectConnection).where(
                    (ProjectConnection.first_id == project.id)
                    | (ProjectConnection.second_id == project.id),
                    ProjectConnection.archived_at.is_(None),
                )
            )
        )
        if peers
        else []
    )
    projects = [
        project,
        *db.scalars(
            select(ProjectRecord)
            .where(
                ProjectRecord.id.in_([counterpart(c, project.id) for c in links]),
                ProjectRecord.archived_at.is_(None),
            )
            .order_by(ProjectRecord.id)
        ),
    ]
    records: dict[str, Row] = {}
    by_num: dict[tuple[str, int], str] = {}
    by_id: dict[tuple[UUID, UUID], str] = {}
    for p in projects:
        cat = catalog(db, p, planning_only=True)
        for kind, table in (("stories", ProjectUserStory), ("epics", ProjectEpic)):
            for local, closed in db.execute(
                select(table.id, table.closed_at).where(
                    table.project_id == p.id, table.archived_at.is_(None)
                )
            ):
                item = cat.local.get(local)
                if item is None:
                    continue
                row = record(
                    p,
                    kind,
                    local,
                    item.number,
                    item.title,
                    item.body or item.original,
                    "closed" if closed else "open",
                )
                row["parents"] = [source_key(p.id, "epics", UUID(k)) for k in item.parents]
                records[source_key(p.id, kind, local)] = row
        parents: dict[UUID, list[str]] = defaultdict(list)
        for issue_id, story in db.execute(
            select(ProjectIssueStory.item_id, ProjectIssueStory.story_id).where(
                ProjectIssueStory.project_id == p.id
            )
        ):
            parents[issue_id].append(source_key(p.id, "stories", story))
        bugs = set(db.scalars(bugs_with_optional_stories(db, p.id)))
        for issue, body in technical_sources(db, p):
            row = record(
                p,
                "issues",
                issue.id,
                issue.issue_number,
                issue.title,
                body,
                issue.issue_state or "open",
            )
            facts = issue.facts or {}
            if issue.id in bugs:
                row["story_optional"] = True
            row.update(
                parents=sorted(parents[issue.id]),
                manual=bool(facts.get("manual_story_links")),
                state_reason=facts.get("github_state_reason"),
            )
            raw = cat.numbers.get(issue.issue_number) if issue.issue_number else None
            refs = {
                (m[1] or m[2] or p.source_repository).casefold() + "#" + m[3]
                for line in STORY_LINE.findall(row["body"] + "\n" + (raw.original if raw else ""))
                for m in STORY_REF.finditer(line)
            }
            row["refs"] = sorted(refs)
            records[source_key(p.id, "issues", issue.id)] = row
    for k, row in records.items():
        by_id[UUID(row["project"]), UUID(row["local"])] = k
        if row["number"]:
            by_num[row["repository"].casefold(), row["number"]] = k
    repo_by_id = {p.id: p.source_repository.casefold() for p in projects}
    connections = {c.id: c for c in links}
    decisions: dict[str, dict[str, str]] = defaultdict(dict)
    for f in db.scalars(
        select(ConnectionFinding)
        .where(
            ConnectionFinding.connection_id.in_(connections),
            ConnectionFinding.action.in_(("link", "create")),
            ConnectionFinding.status.in_(("approved", "dismissed", "pending")),
        )
        .order_by(ConnectionFinding.updated_at, ConnectionFinding.id)
    ):
        peer = counterpart(connections[f.connection_id], f.source_project)
        if f.source_project not in repo_by_id or peer not in repo_by_id:
            continue
        left = by_id.get((f.source_project, f.source_id)) if f.source_id else None
        right = by_id.get((peer, f.target_id)) if f.target_id else None
        left = left or by_num.get((repo_by_id[f.source_project], f.source_number or 0))
        right = right or by_num.get((repo_by_id[peer], f.target_number or 0))
        if not left or not right:
            continue
        if records[left]["kind"] == "stories":
            left, right = right, left
        if records[left]["kind"] == "issues" and records[right]["kind"] == "stories":
            decisions[left][right] = f.status
    cached: dict[str, dict[str, list[str]]] = {}
    local_stories = {
        r["local"]
        for r in records.values()
        if r["project"] == str(project.id) and r["kind"] == "stories"
    }
    if reuse and provider and model and local_stories:
        data = inventory(db, project, local_stories, provider, model, policy)
        for c in data.iter_checkpoints(db, project.id):
            s = str(c.story_id)
            key = source_key(project.id, "stories", UUID(s))
            # The graph can display a GitHub fallback body for a Story with no local
            # description. Only carry a matching decision for the evidence it reviewed.
            if records[key]["body"] != data.stories[s]["description"]:
                continue
            valid = data.known(s, c)
            cached[key] = {
                "checked": sorted(valid),
                "matches": [m["id"] for m in c.matches if m["id"] in valid],
            }
    return {
        "home": str(project.id),
        "matching_evidence": "full_text",
        "projects": len(projects),
        "records": records,
        "decisions": dict(decisions),
        "cached": cached,
    }


def candidates(src: Row, limit: int, *, prerequisites: bool = False) -> tuple[list[Row], int]:
    records: dict[str, Row] = src["records"]
    stories = {k: r for k, r in records.items() if r["kind"] == "stories"}
    docs = {k: terms(r) for k, r in stories.items()}
    postings: dict[str, list[tuple[str, int]]] = defaultdict(list)
    lengths = {k: sum(d.values()) for k, d in docs.items()}
    average = sum(lengths.values()) / max(1, len(docs)) or 1
    for k, doc in docs.items():
        for t, f in doc.items():
            postings[t].append((k, f))
    bridges = review_prerequisites.bridges(src) if prerequisites else {}
    pairs = []
    eligible_count = 0
    for i, issue in sorted(records.items()):
        if issue["kind"] != "issues":
            continue
        eligible = {
            s
            for s, r in stories.items()
            if (r["project"] == src["home"] or issue["project"] == src["home"])
            and (not issue["manual"] or r["project"] != issue["project"] or s in issue["parents"])
            and src["decisions"].get(i, {}).get(s) != "dismissed"
            and (
                not issue.get("story_optional")
                or s in issue["parents"]
                or f"{r['repository'].casefold()}#{r['number']}" in issue["refs"]
                or src["decisions"].get(i, {}).get(s) == "approved"
                or s in bridges.get(i, {})
            )
        }
        eligible_count += len(eligible)
        scores: dict[str, float] = defaultdict(float)
        for t in terms(issue):
            hits = postings.get(t, [])
            weight = math.log1p((len(stories) - len(hits) + 0.5) / (len(hits) + 0.5))
            for s, f in hits:
                if s in eligible:
                    scores[s] += weight * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * lengths[s] / average))
        reasons = {s: "text match" for s in sorted(scores, key=lambda s: (-scores[s], s))[:limit]}
        for s in eligible:
            story = stories[s]
            ref = f"{story['repository'].casefold()}#{story['number']}"
            decision = src["decisions"].get(i, {}).get(s)
            if decision:
                reasons[s] = (
                    "approved connection" if decision == "approved" else "proposed connection"
                )
            elif s in issue["parents"]:
                reasons[s] = "manual parent" if issue["manual"] else "saved parent"
            elif ref in issue["refs"]:
                reasons[s] = "declared parent"
        explicit = any(reason != "text match" for reason in reasons.values())
        if not explicit and max(scores.values(), default=0.0) < 2.0:
            for s in sorted(eligible):
                reasons.setdefault(s, "wider search")
        evidence = bridges.get(i, {})
        # Keep every explicit link and fallback candidate. Within the text shortlist,
        # reserve up to two places for cited prerequisites without increasing its length.
        possible = sorted(
            eligible.intersection(evidence),
            key=lambda s: (-len(evidence[s]), -scores.get(s, 0.0), s),
        )
        new = [s for s in possible if s not in reasons][
            : min(limit, review_prerequisites.BRIDGE_LIMIT)
        ]
        replaceable = sorted(
            (s for s in reasons if reasons[s] == "text match" and s not in evidence),
            key=lambda s: (scores[s], s),
        )
        free = max(0, limit - len(reasons))
        for s in new[: free + len(replaceable)]:
            if not free:
                del reasons[replaceable.pop(0)]
            else:
                free -= 1
            reasons[s] = "documented prerequisite"
        for s, why in sorted(reasons.items()):
            pair: Row = {"issue": i, "story": s, "why": why, "decision": "unchecked"}
            if s in evidence:
                pair["prerequisites"] = evidence[s]
                if why in ("text match", "wider search"):
                    pair["why"] = "documented prerequisite"
            pairs.append(pair)
    return pairs, eligible_count


def wiki_card(row: Row, limit: int) -> Row:
    body: str = row["body"]
    lines = []
    section = ""
    for n, line in enumerate(body.splitlines(), 1):
        line = line.strip()
        if line.startswith("#"):
            section = line.lstrip("# ").lower()
        elif line and not line.startswith("<!--"):
            priority = (
                0
                if "acceptance" in section or PRIORITY.search(line)
                else 1
                if n < 5 or section in ("user story", "description", "scope")
                else 2
            )
            lines.append((priority, n, line))
    chosen = []
    length = 0
    for _, n, line in sorted(lines):
        if length + len(line) + 8 <= limit:
            chosen.append((n, line))
            length += len(line) + 8
    return {
        "revision": row["revision"],
        "text": "\n".join(f"L{n} {s}" for n, s in sorted(chosen)) if chosen else excerpt(body),
        "partial": len(chosen) < len(lines) if chosen else len(body) > 3600,
    }


def wiki_pages(
    db: Session | None, src: Row, keys: set[str], limit: int, save: bool
) -> tuple[dict[str, Row], int]:
    pid = UUID(src["home"])
    fingerprints = {digest([1, k, src["records"][k]["revision"], limit]): k for k in sorted(keys)}
    pages: dict[str, Row] = {}
    signatures = list(fingerprints)
    if db is not None:
        for start in range(0, len(signatures), 500):
            for sig, page in db.execute(
                select(ReviewWikiPage.fingerprint, ReviewWikiPage.page).where(
                    ReviewWikiPage.project_id == pid,
                    ReviewWikiPage.fingerprint.in_(signatures[start : start + 500]),
                )
            ):
                pages[fingerprints[sig]] = page
    reused = len(pages)
    inserts = []
    for sig, k in fingerprints.items():
        if k not in pages:
            pages[k] = wiki_card(src["records"][k], limit)
            inserts.append(
                {"project_id": pid, "fingerprint": sig, "source_key": k, "page": pages[k]}
            )
    if save and db is not None and inserts:
        insert = (
            pg_insert(ReviewWikiPage)
            if db.get_bind().dialect.name == "postgresql"
            else sqlite_insert(ReviewWikiPage)
        )
        db.execute(insert.on_conflict_do_nothing(), inserts)
    return pages, reused


def node_result(
    src: Row, result: Row, step: Row, db: Session | None = None, *, save_pages: bool = False
) -> Row:
    """Execute one pinned deterministic task. Outputs refer to the frozen source by ID."""
    task, params = step["task"], step["params"]
    if step["version"] != 1:
        raise ValueError("Unsupported review evidence task version")
    if task == reviews.GRAPH:
        return {"records": len(src["records"]), "projects": src["projects"]}
    if task == reviews.RANK:
        pairs, possible = candidates(
            src, int(params.get("limit", 8)), prerequisites=bool(params.get("prerequisites"))
        )
        result.update(pairs=pairs, eligible=possible)
        return {"eligible": possible, "selected": len(pairs)}
    if task == reviews.REUSE:
        known = {s: (set(v["checked"]), set(v["matches"])) for s, v in src["cached"].items()}
        count = 0
        for pair in result["pairs"]:
            issue = src["records"][pair["issue"]]
            checked, positive = known.get(pair["story"], (set(), set()))
            # Earlier frozen runs recorded excerpt-based decisions for long documents.
            story = src["records"][pair["story"]]
            if (
                not pair.get("prerequisites")
                and issue["project"] == src["home"]
                and issue["local"] in checked
                and (not src.get("coverage_rubric") or issue["local"] in positive)
                and (
                    src.get("matching_evidence") == "full_text"
                    or (
                        len(issue["body"]) <= EXCERPT_CHARACTERS
                        and len(story["body"]) <= EXCERPT_CHARACTERS
                    )
                )
            ):
                pair["decision"] = "matched" if issue["local"] in positive else "no match"
                count += 1
        return {"reusable": count, "unchecked": len(result["pairs"]) - count}
    if task == reviews.WIKI:
        keys = {p[k] for p in result["pairs"] for k in ("issue", "story")}
        cards, reused = wiki_pages(db, src, keys, int(params.get("characters", 850)), save_pages)
        result["cards"] = cards
        return {"pages": len(keys), "reused_pages": reused}
    raise ValueError("Unknown deterministic review task")
