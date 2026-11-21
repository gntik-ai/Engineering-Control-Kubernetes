"""Native Decisions wire formats for an advisory issue–Story review."""

from __future__ import annotations

import json
from typing import Any

import httpx

from engineering_control.models import openrouter
from engineering_control.models.openai_style import _send
from engineering_control.models.wire import Wire, WireError

RUBRIC = "issue-story-v1"
LINK = "Does the issue describe a concrete contribution to at least one requirement of the Story?"
SCOPE = "Considering all acceptance criteria, how much of the Story does the issue cover?"
CHOICES = {
    "full": "All material requirements are covered.",
    "partial": "At least one concrete requirement is covered, with material work remaining.",
    "unrelated": "No substantive contribution to a Story requirement is established.",
    "unclear": "A contribution is plausible but the available evidence is insufficient.",
}
LEVELS = ["Insufficient evidence", "Indirect evidence", "Explicit source evidence"]
SCORE = "Rate the strength of the original source evidence for your judgment."
Row = dict[str, Any]


def body(provider: str, model: str, system: str, source: str, score: bool, route: Row) -> Row:
    if provider == "openrouter":
        items: Row = {
            "link": {"type": "noul", "instructions": LINK},
            "scope": {"type": "choice", "instructions": SCOPE, "criteria": CHOICES},
        }
        if score:
            items["strength"] = {"type": "score", "instructions": SCORE, "criteria": LEVELS}
        return {
            "model": model,
            "state": system + "\n\n" + source,
            "questions": items,
            **({"provider": route} if route else {}),
        }
    if provider != "openai" or model != openrouter.DIRECT_MODEL:
        raise ValueError("Select a supported native decision model.")
    questions: list[Row] = [
        {"type": "predicate", "name": "link", "instructions": LINK},
        {
            "type": "choice",
            "name": "scope",
            "instructions": SCOPE,
            "choices": [{"value": k, "description": v} for k, v in CHOICES.items()],
        },
    ]
    if score:
        questions.append(
            {
                "type": "score",
                "name": "strength",
                "instructions": SCORE,
                "levels": [{"label": level, "description": level} for level in LEVELS],
            }
        )
    return {
        "model": model,
        "input": [
            {"role": role, "content": [{"type": "input_text", "text": text}]}
            for role, text in (("developer", system), ("user", source))
        ],
        "questions": questions,
        "prompt_cache_options": {"mode": "explicit"},
    }


def probability(value: Any, maximum: int = 1) -> float | None:
    return float(value) if type(value) in (int, float) and 0 <= value <= maximum else None


def judgment(provider: str, answers: Any, score: bool) -> Row:
    by_name: Row = {}
    if provider == "openai" and isinstance(answers, list):
        for item in answers:
            if isinstance(item, dict) and isinstance(name := item.get("name"), str):
                by_name[name] = None if name in by_name else item
    elif provider == "openrouter" and isinstance(answers, dict):
        by_name = answers

    def answer(name: str, kind: str) -> Row:
        entry = by_name.get(name)
        return entry if isinstance(entry, dict) and entry.get("type") == kind else {}

    direct = provider == "openai"
    link = probability(
        answer("link", "predicate" if direct else "noul").get("probability" if direct else "noul")
    )
    picked = answer("scope", "choice").get("choice")
    scope = picked if isinstance(picked, str) and picked in CHOICES else "unclear"
    strength = (
        probability(answer("strength", "score").get("score"), len(LEVELS) - 1) if score else None
    )
    incomplete = link is None or scope == "unclear" or (score and strength is None)
    conflict = link is not None and ((link >= 0.5) != (scope in ("full", "partial")))
    return {
        "scope": scope,
        "link_probability": link,
        "strength": strength,
        "review_reason": "Insufficient or refused answer"
        if incomplete
        else "Link and scope disagree"
        if conflict
        else "Shadow judgment for administrator review",
    }


async def complete(
    http: httpx.AsyncClient, provider: str, base: str, key: str, payload: Row, seconds: float
) -> Wire:
    if provider == "openrouter":
        url = base.removesuffix("/v1") + "/alpha/decisions"
    else:
        url = base + "/decisions"
    response = await _send(http, provider, "POST", url, key, seconds, json=payload)
    ref = response.headers.get("x-request-id")
    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError
        ref = str(data.get("id") or ref or "") or None
        usage = data["usage"]
        inp, out = usage["input_tokens"], usage["output_tokens"]
        detail = usage.get("input_tokens_details") or {}
        cached, written = detail.get("cached_tokens", 0), detail.get("cache_write_tokens", 0)
        if (
            any(type(n) is not int or n < 0 for n in (inp, out, cached, written))
            or cached + written > inp
        ):
            raise ValueError
        if "total_tokens" in usage and usage["total_tokens"] != inp + out:
            raise ValueError
    except (KeyError, AttributeError, ValueError, TypeError) as err:
        raise WireError(
            "unconfirmed", "Decisions returned no usable metering.", provider_ref=ref
        ) from err
    normalized = judgment(provider, data.get("answers"), len(payload["questions"]) == 3)
    return Wire(
        json.dumps(normalized),
        str(data.get("model") or payload["model"]),
        inp - cached,
        out,
        cached,
        written,
        ref,
        openrouter.amount(usage.get("cost")),
        str(data["provider"]) if data.get("provider") else None,
    )
