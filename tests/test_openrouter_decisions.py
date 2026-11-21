"""Exercise the sealed account, both native wire contracts and the durable review checkpoint."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from engineering_control.dashboard import evidence_verdicts, review_evidence, review_flows, stats
from engineering_control.dashboard.workspace_costs import RELEASE_RATES
from engineering_control.db.models import (
    ProjectReviewBudget,
    ProviderAccount,
    ReviewEvidenceJob,
    ReviewFlowRun,
    WorkspaceModelUsage,
)
from engineering_control.models import decisions, openrouter
from engineering_control.models.service import ModelError, Models
from tests.test_dashboard_web import _authenticated_client
from tests.test_models_api import GOOD, SEALING, Asked, question
from tests.test_project_coverage import seed


def row(name: str, mode: str, *, schema: bool = True) -> dict[str, Any]:
    return {
        "id": name,
        "architecture": {"input_modalities": ["text"], "output_modalities": [mode]},
        "pricing": {
            "prompt": "0.000001",
            "completion": "0.000003",
            "input_cache_read": "0.0000001",
            "request": "0.002",
        },
        "top_provider": {"max_completion_tokens": 120},
        "supported_parameters": ["structured_outputs"] if schema else [],
    }


@pytest.mark.asyncio
async def test_router_discovery_native_chat_and_last_success(tmp_path: Path) -> None:
    world = Asked(tmp_path)
    account = world.account("Router", "openrouter")
    requests: list[httpx.Request] = []
    unavailable = False

    def handle(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        assert req.url.host == "openrouter.ai" and req.headers.get("authorization")
        if req.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {}})
        if req.url.path == "/api/v1/models":
            if unavailable:
                return httpx.Response(503)
            mode = req.url.params["output_modalities"]
            entries = (
                [row("~test/qualified:free", "text"), row("test/plain", "text", schema=False)]
                if mode == "text"
                else [row("test/native", "decisions")]
            )
            return httpx.Response(200, json={"data": entries})
        payload = json.loads(req.content)
        if req.url.path == openrouter.CHAT:
            assert payload["provider"] == {
                "allow_fallbacks": False,
                "require_parameters": True,
                "only": ["acme"],
            }
            assert (
                payload["response_format"]["type"] == "json_schema" and payload["max_tokens"] == 120
            )
            return httpx.Response(
                200,
                json={
                    "id": "chat-receipt",
                    "model": "test/reported",
                    "provider": "Acme",
                    "usage": {
                        "prompt_tokens": 16,
                        "completion_tokens": 3,
                        "prompt_tokens_details": {"cached_tokens": 6},
                        "cost": 0.004,
                    },
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": json.dumps(GOOD)}}
                    ],
                },
            )
        assert req.url.path == openrouter.DECISIONS and payload["provider"] == {"only": ["acme"]}
        assert (
            payload["questions"]["link"]["type"] == "noul"
            and "criteria" in payload["questions"]["scope"]
        )
        assert "input" not in payload and "state" in payload
        return httpx.Response(
            200,
            json={
                "id": "decision-receipt",
                "provider": "Acme",
                "model": "test/reported",
                "answers": {
                    "link": {"type": "noul", "noul": 0.82},
                    "scope": {"type": "choice", "choice": "partial"},
                    "strength": {"type": "score", "score": 1.3},
                },
                "usage": {
                    "input_tokens": 18,
                    "output_tokens": 2,
                    "cost": 0,
                    "input_tokens_details": {"cached_tokens": 4},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        models = Models(world.sessions, SEALING, http=http)
        assert (await models.check(account))[0] == "ready"
        assert len(requests) == 3
        with world.sessions() as db:
            assert review_flows.model_choices(db) == [
                {"provider": "openrouter", "model": "~test/qualified:free"}
            ]
            options = review_flows.decision_models(db)
            assert options[0]["model"] == "test/native" and options[0]["rates"] == {
                "input": 1,
                "output": 3,
                "cached": 0.1,
                "request": 0.002,
            }
        standard = replace(
            question(("openrouter", "~test/qualified:free")),
            upstreams="acme",
            allow_fallbacks=False,
        )
        reply = await models.ask(standard, retry=False)
        assert reply.usage["tokens"] == 19 and reply.usage["upstream"] == "Acme"
        assert reply.usage["reported_credits_usd"] == reply.usage["cost"] == 0.004
        native = replace(
            standard,
            choices=(("openrouter", "test/native"),),
            native_score=True,
            schema={"type": "object"},
        )
        reply = await models.ask(native, retry=False)
        assert reply.data["scope"] == "partial" and reply.data["strength"] == 1.3
        assert reply.usage["cost"] == 0 and reply.usage["provider_ref"] == "decision-receipt"
        calls = len(requests)
        with pytest.raises(ModelError, match="supports this task"):
            await models.ask(
                replace(standard, choices=(("openrouter", "test/native"),)), retry=False
            )
        assert len(requests) == calls
        before = world.state(account)
        unavailable = True
        assert (await models.check(account))[0] == "unchecked"
        after = world.state(account)
        assert (
            before["catalog_updated_at"] == after["catalog_updated_at"]
            and before["models"] == after["models"]
        )
    await world.models.aclose()


def test_shadow_task_direct_openai_and_repeated_review(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    data = seed(repo)
    from engineering_control.models.sealing import seal

    with repo.sessions.begin() as db:
        account = ProviderAccount(
            id=uuid4(),
            provider="openai",
            name="Existing account",
            state="ready",
            models=["gpt-6-luna"],
        )
        account.sealed_key = seal(SEALING, account.id, "test-only-credential")
        db.add(account)
        db.add(
            ProjectReviewBudget(project_id=data["project"], token_limit=12000, used=0, reserved=0)
        )
    review_flows.assign(repo.sessions, "falcone", "evidence", "review-native-decisions", None)
    calls = 0

    def handle(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert str(req.url) == "https://api.openai.com/v1/decisions"
        body = json.loads(req.content)
        assert {q["type"] for q in body["questions"]} == {"predicate", "choice"}
        assert not {"provider", "state"} & set(body)
        return httpx.Response(
            200,
            json={
                "id": "direct-1",
                "model": "gpt-6-luna",
                "answers": [
                    {"name": "link", "type": "refusal"},
                    {"name": "scope", "type": "choice", "choice": "partial"},
                ],
                "usage": {
                    "input_tokens": 40,
                    "output_tokens": 0,
                    "total_tokens": 40,
                    "input_tokens_details": {"cached_tokens": 10},
                },
            },
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            models = Models(repo.sessions, SEALING, http=http)
            view = review_evidence.preview(repo.sessions, "falcone", None, "", "", 1, "")
            pair = next(p["id"] for p in view["items"] if p["decision"] == "unchecked")
            first = uuid4()
            review_evidence.start(repo.sessions, "falcone", first, "", "", 12000, pair)
            for _ in range(2):
                assert review_evidence.advance(repo.sessions, first) is None
            call = review_evidence.advance(repo.sessions, first)
            assert (
                call
                and call.arm == "decision"
                and call.question.choices == (("openai", "gpt-6-luna"),)
            )
            assert (
                review_evidence.control(repo.sessions, "falcone", first, "stop")["state"]
                == "stopping"
            )
            reply = await models.ask(call.question, retry=False)
            for _ in range(2):
                review_evidence.settle(repo.sessions, call, reply)
            assert (
                review_evidence.control(repo.sessions, "falcone", first, "resume")["state"]
                == "queued"
            )
            review_evidence.advance(repo.sessions, first)
            detail = evidence_verdicts.detail(repo.sessions, "falcone", first)
            assert detail["native"]["link_probability"] is None
            assert detail["native"]["review_reason"] == "Insufficient or refused answer"
            assert detail["issue"]["body"] and detail["story"]["body"]
            assert evidence_verdicts.listing(repo.sessions, "falcone", False, 1)["total"] == 1
            repeat = uuid4()
            review_evidence.start(repo.sessions, "falcone", repeat, "", "", 12000, pair)
            for _ in range(4):
                assert review_evidence.advance(repo.sessions, repeat) is None
            with repo.sessions() as db:
                assert db.get_one(ReviewEvidenceJob, repeat).result["native"]["cached_from"] == str(
                    first
                )
                assert (
                    db.get_one(ReviewFlowRun, first).settings["decision_model"]["rubric"]
                    == decisions.RUBRIC
                )
                assert db.scalar(select(func.count()).select_from(WorkspaceModelUsage)) == 1
                budget = db.get_one(ProjectReviewBudget, data["project"])
                assert (budget.used, budget.reserved) == (40, 0)
            report = stats.report(repo.sessions, 7, RELEASE_RATES, now=datetime.now(UTC))
            assert report["model_routes"][0]["endpoint"] == "/v1/decisions"
            assert report["model_routes"][0]["tokens"] == 40

    try:
        asyncio.run(exercise())
        assert calls == 1
    finally:
        client.close()


def test_original_and_wiki_cache_keys_follow_router_policy() -> None:
    pair = {"issue": "i", "story": "s"}
    q = question(("openrouter", "test/model"))
    key = review_evidence.comparison_key(pair, [q, q])
    for policies in ({"upstreams": "acme"}, {"allow_fallbacks": False}):
        routed = replace(q, **policies)
        assert review_evidence.comparison_key(pair, [routed, routed]) != key


def test_native_question_contracts_and_uncertain_outcomes() -> None:
    payload = decisions.body("openai", "gpt-6-luna", "rules", "sources", True, {})
    assert payload["questions"][-1]["levels"][2]["label"] == "Explicit source evidence"
    duplicate = [{"name": "link", "type": "predicate", "probability": 1}] * 2
    assert decisions.judgment("openai", duplicate, False)["link_probability"] is None
    conflict = {
        "link": {"type": "noul", "noul": 0.9},
        "scope": {"type": "choice", "choice": "unrelated"},
    }
    assert (
        decisions.judgment("openrouter", conflict, False)["review_reason"]
        == "Link and scope disagree"
    )
    assert decisions.judgment("openrouter", {}, False)["scope"] == "unclear"
    assert openrouter.tariff({"request": "0.02"}) == {"input": 0, "output": 0, "request": 0.02}
    assert openrouter.tariff({"prompt": "-1", "completion": "-1"}) is None
