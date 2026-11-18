"""Asking a model at a provider that accepts chat completions: OpenAI itself, Google's
Gemini API, DeepSeek, Qwen, GLM and Kimi. One request, one answer, no tools: the answer
is asked for as a JSON object, and the platform checks its shape."""

from __future__ import annotations

from typing import Any, Final

import httpx

from engineering_control.models.wire import Wire, WireError, said

EFFORTS: Final = ("low", "medium", "high")


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _words(response: httpx.Response) -> str:
    """What the provider says went wrong, in its own words."""
    try:
        body = response.json()
    except ValueError:
        return said(response.text)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return said(error.get("message") or error.get("code") or error)
        return said(error or body.get("message") or body)
    return said(body)


def _failure(name: str, response: httpx.Response) -> WireError:
    status, words = response.status_code, _words(response)
    if status in (401, 403):
        return WireError("refused", f"{name} does not accept the key: {words}")
    if status == 429:
        try:
            wait: float | None = float(response.headers.get("retry-after") or "")
        except ValueError:
            wait = None
        return WireError("busy", f"{name}'s limit is reached: {words}", retry_after=wait)
    if status >= 500:
        return WireError("busy", f"{name} answered {status}: {words}")
    return WireError("invalid", f"{name} rejected the request ({status}): {words}")


async def _send(
    http: httpx.AsyncClient, name: str, method: str, url: str, key: str, seconds: float, **more: Any
) -> httpx.Response:
    try:
        response = await http.request(
            method, url, headers={"Authorization": f"Bearer {key}"}, timeout=seconds, **more
        )
    except httpx.HTTPError as error:
        raise WireError("busy", f"{name} did not answer: {type(error).__name__}") from error
    if response.status_code != 200:
        raise _failure(name, response)
    return response


def body_of(
    *,
    model: str,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    max_tokens: int,
    limit_name: str = "max_tokens",
    strict: bool = False,
    effort: str | None = None,
) -> dict[str, Any]:
    """One question as a request for a chat completion, asked at once or in a batch."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        limit_name: max_tokens,
        "stream": False,
        # OpenAI and Google hold the model to the shape itself; the others to a JSON
        # object.
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "answer", "strict": True, "schema": schema},
        }
        if strict
        else {"type": "json_object"},
    }
    if effort in EFFORTS:
        body["reasoning_effort"] = effort
    return body


def read(name: str, answer: Any, model: str, max_tokens: int, model_prefix: str = "") -> Wire:
    """What a chat completion says, or why it is no answer."""
    try:
        choice = answer["choices"][0]
        message = choice["message"]
    except (LookupError, TypeError) as error:
        raise WireError("busy", f"{name} answered something that is not an answer.") from error
    reason = str(choice.get("finish_reason") or "")
    if reason == "length":
        raise WireError("cut", f"The answer was longer than the {max_tokens:,} tokens it may have.")
    if reason == "content_filter" or message.get("refusal"):
        raise WireError(
            "declined", f"The model declined to answer: {said(message.get('refusal'))}".rstrip(": ")
        )
    usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
    read_in_all = _count(usage.get("prompt_tokens"))
    # What a model thought before it answered is paid as written. A provider that
    # leaves it out of what was written still counts it in the total.
    written = max(
        _count(usage.get("completion_tokens")), _count(usage.get("total_tokens")) - read_in_all
    )
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    cached = _count(details.get("cached_tokens")) or _count(usage.get("prompt_cache_hit_tokens"))
    # Where a provider says what it wrote to its cache, under either of the two names.
    kept = _count(details.get("cache_write_tokens")) or _count(
        details.get("cache_creation_input_tokens")
    )
    return Wire(
        text=str(message.get("content") or ""),
        model=str(answer.get("model") or model).removeprefix(model_prefix),
        read=max(0, read_in_all - cached),
        written=written,
        cached=cached,
        cache_written=min(kept, max(0, read_in_all - cached)),
    )


async def complete(
    *,
    http: httpx.AsyncClient,
    name: str,
    key: str,
    base_url: str,
    model: str,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    max_tokens: int,
    seconds: float,
    limit_name: str = "max_tokens",
    strict: bool = False,
    effort: str | None = None,
    model_prefix: str = "",
) -> Wire:
    body = body_of(
        model=model,
        system=system,
        prompt=prompt,
        schema=schema,
        max_tokens=max_tokens,
        limit_name=limit_name,
        strict=strict,
        effort=effort,
    )
    url = f"{base_url}/chat/completions"
    try:
        response = await _send(http, name, "POST", url, key, seconds, json=body)
    except WireError as error:
        if not (strict and error.kind == "invalid"):
            raise
        # A provider that will not take the shape as it is written (a rejected request
        # costs nothing) is asked once more for a JSON object: the shape is in the
        # question as well, and the platform checks it. A request that is wrong for
        # another reason, a model that does not exist, is rejected again and said.
        loose = {**body, "response_format": {"type": "json_object"}}
        response = await _send(http, name, "POST", url, key, seconds, json=loose)
    try:
        answer = response.json()
    except ValueError as error:
        raise WireError("busy", f"{name} answered something that is not an answer.") from error
    return read(name, answer, model, max_tokens, model_prefix)


async def models(
    *,
    http: httpx.AsyncClient,
    name: str,
    key: str,
    base_url: str,
    seconds: float = 30,
    model_prefix: str = "",
) -> list[str]:
    """The models the key can ask, as the provider lists them."""
    response = await _send(http, name, "GET", f"{base_url}/models", key, seconds)
    try:
        listed = response.json().get("data")
    except (ValueError, AttributeError) as error:
        raise WireError("busy", f"{name} answered something that is not a list.") from error
    if not isinstance(listed, list):
        raise WireError("busy", f"{name} returned an incomplete model catalog.")
    return sorted(
        {
            entry["id"].removeprefix(model_prefix)
            for entry in listed
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]
        }
    )
