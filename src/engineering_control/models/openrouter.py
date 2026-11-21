"""Authenticated account discovery and USD tariffs from OpenRouter's Models API."""

from __future__ import annotations

import math
import re
from typing import Any

import httpx

from engineering_control.models.openai_style import _send
from engineering_control.models.providers import MODEL_NAME
from engineering_control.models.wire import WireError

CHAT = "/api/v1/chat/completions"
DECISIONS = "/api/alpha/decisions"
TYPES = ("binary", "choice", "score")
DIRECT_MODEL = "gpt-6-luna"
DIRECT_ENDPOINT = "/v1/decisions"
DIRECT_PRICE = {"input": 0.10, "output": 0.0, "cached": 0.01, "cache_write": 0.125}
UPSTREAM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,79}")


def amount(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def tariff(raw: dict[str, Any]) -> dict[str, float] | None:
    fields = {
        "input": "prompt",
        "output": "completion",
        "cached": "input_cache_read",
        "cache_write": "input_cache_write",
        "request": "request",
    }
    rates = {
        key: round(value * (1 if key == "request" else 1_000_000), 12)
        for key, field in fields.items()
        if (value := amount(raw.get(field))) is not None
    }
    if "input" in rates and "output" in rates:
        return rates
    if rates.get("request") and not any(raw.get(k) is not None for k in ("prompt", "completion")):
        return {"input": 0.0, "output": 0.0, **rates}
    return None


def routing(only: str, fallbacks: bool, *, decisions: bool = False) -> dict[str, Any]:
    names = [p.strip() for p in only.split(",") if p.strip()]
    if len(names) > 12 or not all(UPSTREAM.fullmatch(p) for p in names):
        raise ValueError("Use up to twelve comma-separated OpenRouter upstream provider IDs.")
    if decisions:
        if not fallbacks and len(names) != 1:
            raise ValueError("Select one upstream for native Decisions with fallback disabled.")
        return {"only": names} if names else {}
    return {
        "require_parameters": True,
        "allow_fallbacks": fallbacks,
        **({"only": names} if names else {}),
    }


def supports(
    record: dict[str, Any], *, decision: bool = False, score: bool = False, schema: bool = True
) -> bool:
    if decision:
        needed = {"binary", "choice", *(("score",) if score else ())}
        return DECISIONS in record.get("endpoints", []) and needed.issubset(
            record.get("questions", [])
        )
    return CHAT in record.get("endpoints", []) and (
        not schema or "structured_outputs" in record.get("parameters", [])
    )


def describe(model: dict[str, Any], mode: str) -> dict[str, Any] | None:
    arch = model.get("architecture") or {}
    if "text" not in arch.get("input_modalities", []) or mode not in arch.get(
        "output_modalities", []
    ):
        return None
    endpoint = DECISIONS if mode == "decisions" else CHAT
    if "endpoints" in model and endpoint not in (model.get("endpoints") or []):
        return None
    types = model.get("supported_question_types")
    questions = (
        [("binary" if t in ("predicate", "noul") else t) for t in types]
        if isinstance(types, list)
        else list(TYPES)
    )
    return {
        "inputs": arch["input_modalities"],
        "outputs": arch["output_modalities"],
        "endpoints": [endpoint],
        "parameters": model.get("supported_parameters") or [],
        "questions": questions if mode == "decisions" else [],
        "context": model.get("context_length"),
        "max_output": (model.get("top_provider") or {}).get("max_completion_tokens"),
        "rates": tariff(model.get("pricing") or {}),
    }


async def catalog(http: httpx.AsyncClient, base: str, key: str) -> dict[str, Any]:
    # Model listings are public. Only the key endpoint verifies the stored credential.
    await _send(http, "OpenRouter", "GET", base + "/key", key, 30)
    result: dict[str, Any] = {}
    for mode in ("text", "decisions"):
        response = await _send(
            http,
            "OpenRouter",
            "GET",
            base + "/models",
            key,
            30,
            params={"input_modalities": "text", "output_modalities": mode},
        )
        try:
            models = response.json()["data"]
            if not isinstance(models, list) or not models:
                raise ValueError
            for row in models:
                name = row["id"]
                if not isinstance(name, str) or not MODEL_NAME.fullmatch(name):
                    continue
                entry = describe(row, mode)
                if entry:
                    old = result.get(name, {})
                    entry["endpoints"] = sorted(set(old.get("endpoints", []) + entry["endpoints"]))
                    result[name] = entry
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise WireError("invalid", "OpenRouter returned an incomplete model catalog.") from exc
    if not result:
        raise WireError("invalid", "OpenRouter returned no usable model entries.")
    return result
