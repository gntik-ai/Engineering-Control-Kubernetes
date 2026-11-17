"""Batches at Google's Gemini API.

Google takes a batch through its own interface, not the one it shares with OpenAI: the
requests are sent in the call that makes the batch, each a request for generated
content, and what they answered comes back with the batch when it is asked about. An
answer is asked for as JSON; its shape is said in the question and checked by the
platform, as it is for every provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import httpx

from engineering_control.models.wire import Wire, WireError, said

ENDED: Final = (
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
    "JOB_STATE_PARTIALLY_SUCCEEDED",
)
# Why a model gave no text: what Google calls the reasons that are a refusal.
DECLINED: Final = ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "LANGUAGE")


def root_of(base_url: str) -> str:
    """Google's own address, from the one at which it accepts chat completions."""
    return base_url.removesuffix("/").removesuffix("/openai")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _words(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return said(response.text)
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return said(error.get("message") or error.get("status") or error)
    return said(error or body)


async def _send(
    http: httpx.AsyncClient, method: str, url: str, key: str, seconds: float, **more: Any
) -> dict[str, Any]:
    try:
        response = await http.request(
            method, url, headers={"x-goog-api-key": key}, timeout=seconds, **more
        )
    except httpx.HTTPError as error:
        raise WireError("busy", f"Google did not answer: {type(error).__name__}") from error
    status, words = response.status_code, _words(response)
    if status in (401, 403):
        raise WireError("refused", f"Google does not accept the key: {words}")
    if status == 429:
        raise WireError("busy", f"Google's limit is reached: {words}")
    if status >= 500:
        raise WireError("busy", f"Google answered {status}: {words}")
    if status != 200:
        raise WireError("invalid", f"Google rejected the request ({status}): {words}")
    try:
        found = response.json()
    except ValueError as error:
        raise WireError("busy", "Google answered something that is not an answer.") from error
    return found if isinstance(found, dict) else {}


def request_of(*, system: str, prompt: str, max_tokens: int) -> dict[str, Any]:
    """One question as a request for generated content."""
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {"maxOutputTokens": max_tokens, "responseMimeType": "application/json"},
    }


def read(answer: Any, model: str, max_tokens: int) -> Wire:
    """What a response with generated content says, or why it is no answer."""
    if not isinstance(answer, dict):
        raise WireError("busy", "Google answered something that is not an answer.")
    candidates = answer.get("candidates") or []
    feedback = _dict(answer.get("promptFeedback"))
    if not candidates or not isinstance(candidates[0], dict):
        why = said(feedback.get("blockReason") or "")
        raise WireError("declined", f"The model declined to answer: {why}".rstrip(": "))
    first = candidates[0]
    reason = str(first.get("finishReason") or "")
    if reason == "MAX_TOKENS":
        raise WireError("cut", f"The answer was longer than the {max_tokens:,} tokens it may have.")
    if reason in DECLINED:
        raise WireError("declined", f"The model declined to answer: {reason.lower()}")
    content = _dict(first.get("content"))
    text = "".join(
        str(part.get("text") or "")
        for part in content.get("parts") or []
        # What the model thought on the way is not its answer.
        if isinstance(part, dict) and not part.get("thought")
    )
    usage = _dict(answer.get("usageMetadata"))
    read_in_all = _count(usage.get("promptTokenCount"))
    cached = min(read_in_all, _count(usage.get("cachedContentTokenCount")))
    return Wire(
        text=text,
        model=str(answer.get("modelVersion") or model).removeprefix("models/"),
        read=read_in_all - cached,
        # What it thought before it answered is paid as written.
        written=_count(usage.get("candidatesTokenCount")) + _count(usage.get("thoughtsTokenCount")),
        cached=cached,
    )


def _when(value: Any) -> datetime:
    try:
        found = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, UTC)
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _state(batch: dict[str, Any]) -> dict[str, Any]:
    # A batch that is listed comes as it is; one that is asked about comes wrapped, with
    # what is said of it under metadata.
    metadata = _dict(batch.get("metadata")) or batch
    stats = _dict(metadata.get("batchStats"))
    status = str(metadata.get("state") or "")
    total = _count(stats.get("requestCount"))
    done = _count(stats.get("successfulRequestCount"))
    failed = _count(stats.get("failedRequestCount"))
    return {
        "id": str(batch.get("name") or metadata.get("name") or ""),
        "ended": status in ENDED or bool(batch.get("done")),
        "status": status,
        "created_at": _when(metadata.get("createTime")),
        "waiting": _count(stats.get("pendingRequestCount")) or max(0, total - done - failed),
        "succeeded": done,
        "errored": failed,
        "cancelled": 0,
        "expired": 0,
        "total": total,
        "label": metadata.get("displayName"),
        "raw": batch,
    }


async def create(
    *,
    http: httpx.AsyncClient,
    key: str,
    base_url: str,
    model: str,
    requests: list[tuple[str, dict[str, Any]]],
    label: str,
    seconds: float = 120,
) -> str:
    """Hand Google the requests as one batch. Returns the batch's name there."""
    made = await _send(
        http,
        "POST",
        f"{root_of(base_url)}/models/{model}:batchGenerateContent",
        key,
        seconds,
        json={
            "batch": {
                "display_name": label,
                "input_config": {
                    "requests": {
                        "requests": [
                            {"request": request, "metadata": {"key": name}}
                            for name, request in requests
                        ]
                    }
                },
            }
        },
    )
    name = str(made.get("name") or "")
    if not name.startswith("batches/"):
        raise WireError("busy", "Google made a batch and gave it no name.")
    return name


async def state(
    *, http: httpx.AsyncClient, key: str, base_url: str, batch_id: str
) -> dict[str, Any]:
    return _state(await _send(http, "GET", f"{root_of(base_url)}/{batch_id}", key, 120))


async def recent(*, http: httpx.AsyncClient, key: str, base_url: str) -> list[dict[str, Any]]:
    listed = await _send(
        http, "GET", f"{root_of(base_url)}/batches", key, 60, params={"pageSize": 20}
    )
    entries = next((value for value in listed.values() if isinstance(value, list)), [])
    return [_state(entry) for entry in entries if isinstance(entry, dict)]


async def cancel(*, http: httpx.AsyncClient, key: str, base_url: str, batch_id: str) -> None:
    await _send(http, "POST", f"{root_of(base_url)}/{batch_id}:cancel", key, 60)


def results(
    where: dict[str, Any], model: str, tokens: dict[str, int], order: list[str]
) -> dict[str, Wire | WireError]:
    """What a batch that has ended answered, by the name of each request. The answers
    come with the batch's state, in the order the requests were sent, each with the
    name it was given where Google says it."""
    raw = _dict(where.get("raw"))
    problem = _dict(raw.get("error")) or None
    if problem is not None or where.get("status") == "JOB_STATE_FAILED":
        why = f"Google gave the batch up: {said((problem or {}).get('message') or '')}"
        return {name: WireError("busy", why.rstrip(": ")) for name in order}
    response = _dict(raw.get("response"))
    inlined = response.get("inlinedResponses")
    if isinstance(inlined, dict):
        inlined = inlined.get("inlinedResponses")
    found: dict[str, Wire | WireError] = {}
    for position, entry in enumerate(inlined if isinstance(inlined, list) else []):
        if not isinstance(entry, dict):
            continue
        metadata = _dict(entry.get("metadata"))
        name = str(metadata.get("key") or (order[position] if position < len(order) else ""))
        if name not in tokens:
            continue
        failed = _dict(entry.get("error")) or None
        if failed is not None:
            status = _count(failed.get("code"))
            kind = "invalid" if 400 <= status < 500 and status != 429 else "busy"
            found[name] = WireError(kind, f"Google could not answer: {said(failed.get('message'))}")
            continue
        try:
            found[name] = read(entry.get("response"), model, tokens[name])
        except WireError as error:
            found[name] = error
    return found
