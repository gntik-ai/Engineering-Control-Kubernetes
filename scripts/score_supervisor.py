"""Score the Supervisor's three issue-selection methods using configured Accounts.

Run explicitly with --project and a --run-id UUID. Reusing that ID will reuse completed
observations and skip interrupted attempts. The only production writes are evaluation
receipts and normal project usage accounting. No issue or assignment is changed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from engineering_control.config import get_settings
from engineering_control.dashboard import review_budget, review_flows
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    active_project,
)
from engineering_control.db.models import ProviderAccount, ReviewFlowRun
from engineering_control.models.openrouter import DIRECT_MODEL
from engineering_control.models.sealing import read_sealing_key
from engineering_control.models.service import ModelError, Models
from engineering_control.supervisor import advice

CANDIDATES = (
    (
        "cookie",
        "Expired session renewal",
        "Renewal now loops indefinitely after a session expires. Previously one sign-in recovered it.",
    ),
    (
        "export",
        "Introduce CSV usage export",
        "Administrators request a first-time download of daily service usage. There is no export today.",
    ),
    (
        "link",
        "Existing issue link disappears",
        "Saving a Story removes its existing technical issue. The relation must survive saving.",
    ),
    (
        "billing",
        "Billing retry controls",
        "Let the admin choose how many times to retry importing billing records, a new option.",
    ),
    (
        "filters",
        "Keep the chosen project filter",
        "Today the list resets on navigation. The design calls for a new, persistent filter.",
    ),
)
# Each independent reference follows directly from the stated requirement. The model sees
# only the request and candidate source text, never an expected ID or classification.
CASES = (
    (
        "expired-session",
        "Which issue fixes the regression where I can't sign in again after my cookie expires?",
        "cookie",
        "bug",
    ),
    (
        "daily-download",
        "I'd like daily consumption as a downloadable spreadsheet.",
        "export",
        "enhancement",
    ),
    (
        "lost-relation",
        "Find the technical ticket for the issue that vanishes when I save its user story.",
        "link",
        "bug",
    ),
    ("underspecified", "Can you handle the next item for me?", None, "unclear"),
)


async def evaluate(project: str, run_id: UUID) -> dict[str, Any]:
    settings = get_settings()
    engine = create_engine(settings.database_url.get_secret_value(), hide_parameters=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    models = Models(sessions, read_sealing_key(settings.model_keys_key_file))
    try:
        with sessions() as db:
            pid = active_project(db, project).id
            catalog = {(r["provider"], r["model"]) for r in review_flows.model_choices(db)}
            available = review_flows.decision_models(db)
            routes: list[tuple[str, str, str]] = []
            # Prefer an inexpensive Accounts text model that supports structured responses.
            eligible = []
            for provider, model in catalog:
                if provider not in ("anthropic", "openai") or not model.startswith(
                    ("claude-", "gpt-")
                ):
                    continue
                if any(
                    word in model
                    for word in (
                        "audio",
                        "image",
                        "tts",
                        "realtime",
                        "search",
                        "transcribe",
                        "codex",
                    )
                ):
                    continue
                quoted = db.scalars(
                    select(ProviderAccount.prices).where(
                        ProviderAccount.provider == provider,
                        ProviderAccount.archived_at.is_(None),
                        ProviderAccount.paused.is_(False),
                    )
                )
                rates = [q[model] for q in quoted if model in (q or {})]
                if rates and all(
                    0 <= r.get("input", 1000) <= 6 and 0 <= r.get("output", 1000) <= 35
                    for r in rates
                ):
                    eligible.append(
                        (max(r["input"] + 0.25 * r["output"] for r in rates), provider, model)
                    )
            if eligible:
                _, provider, model = min(eligible)
                routes.extend((method, provider, model) for method in ("standard", "rerank"))
            for provider in ("openai", "openrouter"):
                usable = [
                    r
                    for r in available
                    if r["provider"] == provider
                    and r.get("rates")
                    and 0 <= r["rates"].get("input", 1000) <= 6
                    and 0 <= r["rates"].get("output", 1000) <= 35
                    and r["rates"].get("request", 0) <= 0.005
                ]
                if usable:
                    choice = min(
                        usable,
                        key=lambda r: (r["model"] != DIRECT_MODEL, r["rates"]["input"], r["model"]),
                    )
                    routes.append(("decision", provider, choice["model"]))
        result: dict[str, Any] = {"project": project, "cases": len(CASES), "routes": []}
        for method, provider, model in routes:
            route: dict[str, Any] = {
                "method": method,
                "provider": provider,
                "model": model,
                "observations": [],
            }
            result["routes"].append(route)
            for index, (case, prompt, expected, category) in enumerate(CASES):
                rid = uuid5(run_id, f"{project}:{method}:{provider}:{model}:{case}")
                with sessions.begin() as db:
                    saved = db.get(ReviewFlowRun, rid)
                    if saved is not None:
                        route["observations"].append(
                            (saved.findings or {}).get("observation")
                            or {"case": case, "error": "previous attempt interrupted"}
                        )
                        continue
                    review_budget.require_available(db, pid)
                    db.add(
                        ReviewFlowRun(
                            id=rid,
                            project_id=pid,
                            action="supervisor",
                            flow_key="review-supervisor",
                            flow_name=f"Supervisor benchmark · {method}",
                            version=0,
                            settings={},
                            findings={"benchmark": str(run_id)},
                        )
                    )
                ordered = CANDIDATES[index:] + CANDIDATES[:index]
                candidates = [
                    {"id": key, "title": title, "text": description}
                    for key, title, description in ordered
                ]
                try:
                    answer = await advice.compare(
                        sessions,
                        models,
                        pid,
                        rid,
                        {
                            "request": prompt,
                            "candidates": candidates,
                            "method": method,
                            "provider": provider,
                            "model": model,
                        },
                    )
                    calls = answer["calls"]
                    observed = {
                        "case": case,
                        "correct": (answer["item"], answer["category"]) == (expected, category),
                        "item": answer["item"],
                        "category": answer["category"],
                        "calls": len(calls),
                        "tokens": sum(c["usage"].get("tokens", 0) for c in calls),
                        "estimated_usd": round(sum(c["usage"].get("cost", 0) for c in calls), 7)
                        if all("cost" in c["usage"] for c in calls)
                        else None,
                    }
                except (ProjectConflict, ProjectInvalid, ModelError) as e:
                    observed = {"case": case, "error": getattr(e, "label", type(e).__name__)}
                with sessions.begin() as db:
                    saved = db.get(ReviewFlowRun, rid)
                    saved.outcome = "failed" if "error" in observed else "succeeded"
                    saved.finished_at = datetime.now(UTC)
                    saved.findings = {"benchmark": str(run_id), "observation": observed}
                route["observations"].append(observed)
                if "error" in observed:
                    break
        return result
    finally:
        await models.aclose()
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True, type=UUID)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(evaluate(args.project, args.run_id)), sort_keys=True))
