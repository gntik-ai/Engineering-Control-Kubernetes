"""Batches at a provider that takes them as OpenAI does: OpenAI itself, Qwen and Kimi.

The requests are written into a file, a line a request, and the file is uploaded; a
batch is made of the file; when it has ended, what it answered is another file, a line
an answer, and what could not be answered a third. Each request is a chat completion,
as the provider is asked at once.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from engineering_control.models import openai_style
from engineering_control.models.wire import Wire, WireError, said

# The label a batch carries at the provider, where the provider keeps one: the name the
# platform's own record of the batch has.
LABEL: Final = "engineering_control"
ENDED: Final = ("completed", "expired", "cancelled", "failed")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _json(name: str, response: httpx.Response) -> dict[str, Any]:
    try:
        found = response.json()
    except ValueError as error:
        raise WireError("busy", f"{name} answered something that is not an answer.") from error
    if not isinstance(found, dict):
        raise WireError("busy", f"{name} answered something that is not an answer.")
    return found


def _state(batch: dict[str, Any]) -> dict[str, Any]:
    counts = _dict(batch.get("request_counts"))
    total, done, failed = (_count(counts.get(key)) for key in ("total", "completed", "failed"))
    status = str(batch.get("status") or "")
    metadata = _dict(batch.get("metadata"))
    problems = _dict(batch.get("errors"))
    first = next((entry for entry in problems.get("data") or [] if isinstance(entry, dict)), {})
    try:
        made = datetime.fromtimestamp(float(batch.get("created_at") or 0), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        made = datetime.fromtimestamp(0, UTC)
    return {
        "id": str(batch.get("id") or ""),
        "ended": status in ENDED,
        "status": status,
        "created_at": made,
        "waiting": max(0, total - done - failed),
        "succeeded": done,
        "errored": failed,
        "cancelled": 0,
        "expired": 0,
        "total": total,
        "label": metadata.get(LABEL),
        "input_file": batch.get("input_file_id"),
        "output_file": batch.get("output_file_id"),
        "error_file": batch.get("error_file_id"),
        # Why the provider would not take the batch at all, when it would not.
        "problem": said(first.get("message") or first.get("code") or ""),
    }


async def create(
    *,
    http: httpx.AsyncClient,
    name: str,
    key: str,
    base_url: str,
    requests: list[tuple[str, dict[str, Any]]],
    label: str,
    endpoint: str = "/v1/chat/completions",
    seconds: float = 120,
) -> str:
    """Upload the requests as a file and make a batch of it. Returns the batch's name."""
    lines = "\n".join(
        json.dumps({"custom_id": custom, "method": "POST", "url": endpoint, "body": body})
        for custom, body in requests
    )
    uploaded = await openai_style._send(
        http,
        name,
        "POST",
        f"{base_url}/files",
        key,
        seconds,
        data={"purpose": "batch"},
        files={"file": ("questions.jsonl", lines.encode(), "application/jsonl")},
    )
    file_id = _json(name, uploaded).get("id")
    if not file_id:
        raise WireError("busy", f"{name} took the file of questions and gave it no name.")
    made = await openai_style._send(
        http,
        name,
        "POST",
        f"{base_url}/batches",
        key,
        seconds,
        json={
            "input_file_id": file_id,
            "endpoint": endpoint,
            "completion_window": "24h",
            "metadata": {LABEL: label},
        },
    )
    batch_id = _json(name, made).get("id")
    if not batch_id:
        raise WireError("busy", f"{name} made a batch and gave it no name.")
    return str(batch_id)


async def state(
    *, http: httpx.AsyncClient, name: str, key: str, base_url: str, batch_id: str
) -> dict[str, Any]:
    response = await openai_style._send(
        http, name, "GET", f"{base_url}/batches/{batch_id}", key, 60
    )
    return _state(_json(name, response))


async def recent(
    *, http: httpx.AsyncClient, name: str, key: str, base_url: str
) -> list[dict[str, Any]]:
    response = await openai_style._send(
        http, name, "GET", f"{base_url}/batches", key, 60, params={"limit": 20}
    )
    listed = _json(name, response).get("data") or []
    return [_state(entry) for entry in listed if isinstance(entry, dict)]


async def cancel(
    *, http: httpx.AsyncClient, name: str, key: str, base_url: str, batch_id: str
) -> None:
    await openai_style._send(http, name, "POST", f"{base_url}/batches/{batch_id}/cancel", key, 60)


async def _lines(
    http: httpx.AsyncClient, name: str, key: str, base_url: str, file_id: Any
) -> list[dict[str, Any]]:
    if not file_id:
        return []
    response = await openai_style._send(
        http, name, "GET", f"{base_url}/files/{file_id}/content", key, 300
    )
    found = []
    for line in response.text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            found.append(entry)
    return found


def _one(
    name: str, entry: dict[str, Any], model: str, max_tokens: int, model_prefix: str
) -> Wire | WireError:
    """What one line of a batch's answers says."""
    response = _dict(entry.get("response")) or None
    error = _dict(entry.get("error")) or None
    if response is not None and _count(response.get("status_code")) == 200:
        try:
            return openai_style.read(name, response.get("body"), model, max_tokens, model_prefix)
        except WireError as failed:
            return failed
    if error is not None and str(error.get("code") or "") in ("batch_expired", "batch_cancelled"):
        return WireError("late", f"{name} did not answer this before the batch ended.")
    words = ""
    status = _count(response.get("status_code")) if response is not None else 0
    body = response.get("body") if response is not None else None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        words = said(body["error"].get("message") or body["error"].get("code"))
    words = words or said((error or {}).get("message") or (error or {}).get("code"))
    kind = "busy" if status == 429 or status >= 500 or not status else "invalid"
    return WireError(kind, f"{name} could not answer: {words}".rstrip(": "))


async def results(
    *,
    http: httpx.AsyncClient,
    name: str,
    key: str,
    base_url: str,
    where: dict[str, Any],
    model: str,
    tokens: dict[str, int],
    model_prefix: str = "",
) -> dict[str, Wire | WireError]:
    """What a batch that has ended answered, by the name of each request. ``where`` is
    the batch's state, which names the files its answers are in."""
    if where.get("status") == "failed":
        why = f"{name} did not take the batch: {where.get('problem') or 'it does not say why'}"
        return {custom: WireError("invalid", why) for custom in tokens}
    found: dict[str, Wire | WireError] = {}
    for file_id in (where.get("error_file"), where.get("output_file")):
        for entry in await _lines(http, name, key, base_url, file_id):
            custom = str(entry.get("custom_id") or "")
            if custom in tokens:
                found[custom] = _one(name, entry, model, tokens[custom], model_prefix)
    # The file of questions has done its work: it is not left with the provider.
    if where.get("input_file"):
        try:
            await http.request(
                "DELETE",
                f"{base_url}/files/{where['input_file']}",
                headers={"Authorization": f"Bearer {key}"},
                timeout=30,
            )
        except httpx.HTTPError:
            pass
    return found
