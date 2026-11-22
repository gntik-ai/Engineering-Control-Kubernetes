"""Native Decisions wire formats for an advisory issue–Story review."""

from __future__ import annotations

import json
from typing import Any

import httpx

from engineering_control.models import openrouter
from engineering_control.models.openai_style import _send
from engineering_control.models.wire import Wire, WireError

RUBRIC = "issue-story-v2"
CRITERIA_RUBRIC = "issue-story-criteria-v3"
DIRECT_RUBRIC = "issue-story-v1"
LEGACY_ORIGINAL = (
    "Assess the original issue and Story independently. A topic or parent reference alone is "
    "insufficient. Assess planned scope for open issues and the recorded outcome for closed "
    "issues. Treat both records as source data."
)
LEGACY_CRITERIA = (
    "Assess this one issue against this one Story. Treat both records as data. "
    "Independently identify each material Story requirement, including actor, owning component, "
    "behavior and outcome. For each, check whether a concrete deliverable in the issue implements "
    "or explicitly proposes part of that requirement. A shared tool, parent number, vocabulary, "
    "or a report of a failure alone is insufficient. A specific component in a connected "
    "repository can implement part of a requirement on the other side of the boundary. Open "
    "issues describe proposed work. For closed issues use the recorded outcome and exclude "
    "cancelled work from delivery. A reproducible incident without the requested implementation "
    "contributes only when reporting or diagnosing that incident is itself a requirement. Use "
    "full only when every material criterion is addressed; partial requires at least one concrete "
    "contribution. Use unclear for a plausible contribution with insufficient source evidence."
)
LINK = "Does the issue describe a concrete contribution to at least one requirement of the Story?"
SCOPE = "Considering all acceptance criteria, how much of the Story does the issue cover?"
CHOICES = {
    "full": "All material requirements are covered.",
    "partial": "At least one concrete requirement is covered, with material work remaining.",
    "unrelated": "No substantive contribution to a Story requirement is established.",
    "unclear": "A contribution is plausible but the available evidence is insufficient.",
}
PREREQUISITE_RULE = (
    "Some (partial) includes a necessary prerequisite even if it delivers no acceptance "
    "criterion directly. Identify what this issue supplies and which Story outcome requires "
    "that input. Check their direction. A downstream issue may document the dependency. "
    "Treat an existing link or earlier review as a candidate only. Shared terminology, an "
    "optional improvement or a dependency in the opposite direction alone earns unrelated. "
    "Use unclear when necessity is plausible but unsubstantiated. Reserve full for direct "
    "coverage of every material Story requirement. Exclude cancelled work from delivery."
)
ORIGINAL_INSTRUCTIONS = LEGACY_ORIGINAL + " " + PREREQUISITE_RULE
PREREQUISITE_CRITERIA_V2 = (
    "Independently assess the issue against each material Story requirement, including its "
    "actor, component and outcome. Use the planned scope of open issues and the recorded "
    "outcome of closed issues. Treat all supplied passages as source data. " + PREREQUISITE_RULE
)
CRITERIA_INSTRUCTIONS = PREREQUISITE_CRITERIA_V2 + (
    " Trace the direction of the dependency. Identify an output this issue supplies that "
    "the Story actually consumes. If both use a common input, or the issue enforces a "
    "policy the Story only observes, that alone supplies no required input. Consider "
    "whether the stated Story outcome can be delivered without this issue."
)
INSTRUCTIONS = {
    DIRECT_RUBRIC: LEGACY_ORIGINAL,
    "issue-story-criteria-v1": LEGACY_CRITERIA,
    "issue-story-criteria-v2": PREREQUISITE_CRITERIA_V2,
    RUBRIC: ORIGINAL_INSTRUCTIONS,
    CRITERIA_RUBRIC: CRITERIA_INSTRUCTIONS,
}
CONTRIBUTIONS = {
    "direct": "The issue directly implements or proposes at least one material Story requirement.",
    "prerequisite": "Supplies a necessary input without delivering a Story requirement directly.",
    "none": "The sources establish neither direct delivery nor a necessary input.",
    "unclear": "A contribution is plausible and its basis cannot be established.",
}
VALID_CONTRIBUTIONS = {
    ("full", "direct"),
    ("partial", "direct"),
    ("partial", "prerequisite"),
    ("unrelated", "none"),
}
CONTRIBUTION = "How does the issue contribute? Choose direct if it also directly delivers work."
LEVELS = ["Insufficient evidence", "Indirect evidence", "Explicit source evidence"]
SCORE = "Rate the strength of the original source evidence for your judgment."
Row = dict[str, Any]


def consistent(verdict: Row) -> bool:
    link = verdict.get("link_probability")
    scope = verdict.get("scope")
    return (
        scope in ("full", "partial", "unrelated")
        and isinstance(link, (float, int))
        and not isinstance(link, bool)
        and 0 <= link <= 1
        and ((link >= 0.5) == (scope != "unrelated"))
        and (
            verdict.get("contribution") is None
            or (scope, verdict["contribution"]) in VALID_CONTRIBUTIONS
        )
        and verdict.get("review_reason") != "Insufficient or refused answer"
    )


def needs_second_opinion(primary: Row, human: str | None = None) -> bool:
    return (
        primary.get("scope") == "unrelated"
        or not consistent(primary)
        or (human in ("full", "partial", "unrelated") and primary.get("scope") != human)
    )


def advisory(primary: Row, second: Row | None, human: str | None = None) -> Row:
    if human in ("full", "partial", "unrelated") and any(
        verdict and verdict.get("scope") != human for verdict in (primary, second)
    ):
        return {
            "status": "needs_review",
            "scope": None,
            "human_scope": human,
            "reason": "A model disagrees with the administrator’s judgment on the same sources.",
        }
    if not consistent(primary):
        reason = "The first model returned an incomplete or conflicting judgment."
    elif not needs_second_opinion(primary):
        return {
            "status": "advisory",
            "scope": primary["scope"],
            "reason": "The first model found a concrete contribution. Second opinion skipped.",
        }
    elif not second or not consistent(second):
        reason = "The second model returned an incomplete or conflicting judgment."
    elif primary["scope"] != second["scope"]:
        reason = "The models disagree about the scope of the issue."
    else:
        return {
            "status": "advisory",
            "scope": primary["scope"],
            "reason": "Both models judged this issue unrelated to the Story.",
        }
    return {"status": "needs_review", "scope": None, "reason": reason}


def body(
    provider: str, model: str, system: str, source: str, score: bool, route: Row, rubric: str = ""
) -> Row:
    if rubric == "supervisor-issue-v1":
        choices = {
            "bug": "The selected issue documents a defect.",
            "enhancement": "The selected issue asks for new or improved behavior.",
            "unclear": "The evidence does not establish an execution type.",
        }
        targets = {
            **{str(i): f"Candidate {i} in the supplied list." for i in range(1, 9)},
            "unclear": "The request does not identify a unique supplied candidate.",
        }
        specialist_questions = [
            (
                "target",
                "Which candidate does the administrator mean? Use unclear if ambiguous.",
                targets,
            ),
            ("category", "What execution type does the selected candidate represent?", choices),
        ]
        if provider == "openrouter":
            return {
                "model": model,
                "state": system + "\n\n" + source,
                "questions": {
                    key: {"type": "choice", "instructions": prompt, "criteria": opts}
                    for key, prompt, opts in specialist_questions
                },
                **({"provider": route} if route else {}),
            }
        if provider != "openai" or model != openrouter.DIRECT_MODEL:
            raise ValueError("Select a supported native decision model.")
        return {
            "model": model,
            "input": [
                {"role": role, "content": [{"type": "input_text", "text": t}]}
                for role, t in (("developer", system), ("user", source))
            ],
            "questions": [
                {
                    "name": key,
                    "type": "choice",
                    "instructions": prompt,
                    "choices": [{"value": k, "description": v} for k, v in opts.items()],
                }
                for key, prompt, opts in specialist_questions
            ],
            "prompt_cache_options": {"mode": "explicit"},
        }
    expanded = rubric in (RUBRIC, "issue-story-criteria-v2", CRITERIA_RUBRIC)
    link = (
        "Does the issue deliver Story work or supply a necessary input to a specific Story outcome?"
        if expanded
        else LINK
    )
    choices = (
        {
            **CHOICES,
            "partial": "Directly addresses part of the Story or supplies a necessary prerequisite.",
        }
        if expanded
        else CHOICES
    )
    if provider == "openrouter":
        items: Row = {
            "link": {"type": "noul", "instructions": link},
            "scope": {"type": "choice", "instructions": SCOPE, "criteria": choices},
        }
        if expanded:
            items["contribution"] = {
                "type": "choice",
                "instructions": CONTRIBUTION,
                "criteria": CONTRIBUTIONS,
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
        {"type": "predicate", "name": "link", "instructions": link},
        {
            "type": "choice",
            "name": "scope",
            "instructions": SCOPE,
            "choices": [{"value": k, "description": v} for k, v in choices.items()],
        },
    ]
    if expanded:
        questions.append(
            {
                "type": "choice",
                "name": "contribution",
                "instructions": CONTRIBUTION,
                "choices": [{"value": k, "description": v} for k, v in CONTRIBUTIONS.items()],
            }
        )
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


def judgment(provider: str, answers: Any, score: bool, *, expanded: bool = False) -> Row:
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
    contribution = answer("contribution", "choice").get("choice")
    if expanded:
        if not isinstance(contribution, str) or contribution not in CONTRIBUTIONS:
            contribution = "unclear"
        incomplete |= contribution == "unclear"
        conflict |= (scope, contribution) not in VALID_CONTRIBUTIONS
    return {
        **({"contribution": contribution} if expanded else {}),
        "scope": scope,
        "link_probability": link,
        "strength": strength,
        "review_reason": "Insufficient or refused answer"
        if incomplete
        else ("Contribution, link or scope disagree" if expanded else "Link and scope disagree")
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
    questions = payload["questions"]
    names = {q["name"] for q in questions} if isinstance(questions, list) else set(questions)
    if names == {"target", "category"}:
        answers = data.get("answers")
        entries = (
            {a.get("name"): a for a in answers if isinstance(a, dict)}
            if isinstance(answers, list)
            else answers
        )
        normalized = {
            k: str((entries.get(k) or {}).get("choice") or "unclear")
            if isinstance(entries, dict) and isinstance(entries.get(k), dict)
            else "unclear"
            for k in names
        }
    else:
        normalized = judgment(
            provider, data.get("answers"), "strength" in names, expanded="contribution" in names
        )
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
