"""The models an agent can work with, and how hard each can reason.

One list for everything that starts an agent: the supervisor's turns and the steps of a
flow. It is what the clients pinned in the sandbox image take (images/sandbox/node-tools):
for Claude the names its client resolves to the latest model of a family, for ChatGPT the
catalogue built into the Codex client, with the levels each model has. Another name can
always be written; the client then says whether it knows it. When the pinned clients are
replaced, this list is brought in step with them.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

MODEL_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,59}$")

_LEVELS: Final = ("low", "medium", "high", "xhigh", "max")
EFFORTS: Final[dict[str, tuple[str, ...]]] = {
    "claude": _LEVELS,
    "chatgpt": (*_LEVELS, "ultra"),
}
MODELS: Final[dict[str, tuple[dict[str, Any], ...]]] = {
    "claude": (
        {"name": "fable", "title": "Fable", "summary": "The latest Fable model."},
        {"name": "opus", "title": "Opus", "summary": "The latest Opus model."},
        {"name": "sonnet", "title": "Sonnet", "summary": "The latest Sonnet model."},
        {"name": "haiku", "title": "Haiku", "summary": "The latest Haiku model: the fastest."},
    ),
    "chatgpt": (
        {
            "name": "gpt-6-astra",
            "title": "GPT-6-Astra",
            "summary": "Frontier intelligence for the most demanding work.",
        },
        {
            "name": "gpt-6.1-sol",
            "title": "GPT-6.1-Sol",
            "summary": "Latest workhorse model for coding and everyday work.",
        },
        {"name": "gpt-6.1-terra", "title": "GPT-6.1 Terra", "summary": "A balanced model."},
        {
            "name": "gpt-6-sol",
            "title": "GPT-6-Sol",
            "summary": "Previous generation workhorse model.",
        },
        {
            "name": "gpt-6-luna",
            "title": "GPT-6-Luna",
            "summary": "Fast and affordable model for easier tasks.",
            "efforts": _LEVELS,
        },
        {
            "name": "gpt-5.6-sol",
            "title": "GPT-5.6-Sol",
            "summary": "Older generation workhorse model.",
        },
        {
            "name": "gpt-5.6-terra",
            "title": "GPT-5.6-Terra",
            "summary": "Older balanced model for straightforward work.",
        },
        {
            "name": "gpt-5.6-luna",
            "title": "GPT-5.6-Luna",
            "summary": "Older fast and efficient model.",
            "efforts": _LEVELS,
        },
        {
            "name": "gpt-5.5",
            "title": "GPT-5.5",
            "summary": "Legacy coding model.",
            "efforts": ("low", "medium", "high", "xhigh"),
        },
    ),
}


def choices() -> dict[str, Any]:
    """What the console offers for each provider: its models and its reasoning levels."""
    return {
        provider: {
            "models": [
                {**model, "efforts": list(model.get("efforts") or EFFORTS[provider])}
                for model in MODELS[provider]
            ],
            "efforts": list(EFFORTS[provider]),
        }
        for provider in EFFORTS
    }


def problem(provider: str, model: Any, effort: Any) -> str | None:
    """What is wrong with a model and a level chosen for a provider, or None."""
    if provider not in EFFORTS:
        return f"There is no provider {provider}"
    name = str(model or "").strip()
    level = str(effort or "").strip()
    if name and not MODEL_NAME.fullmatch(name):
        return f"{name} is not a model's name"
    if level and level not in EFFORTS[provider]:
        return f"{level} is not a reasoning level of {provider}"
    known = next((item for item in MODELS[provider] if item["name"] == name), None)
    if known is not None and level and level not in (known.get("efforts") or EFFORTS[provider]):
        return f"{known['title']} has no reasoning level {level}"
    return None


def flags(provider: str, model: str | None, effort: str | None) -> list[str]:
    """What the provider's client is given to work with that model at that level. Nothing
    chosen is nothing said: the client then chooses by itself."""
    said: list[str] = []
    if provider == "claude":
        if model:
            said += ["--model", model]
        if effort:
            said += ["--effort", effort]
    elif provider == "chatgpt":
        if model:
            said += ["-m", model]
        if effort:
            said += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
    return said
