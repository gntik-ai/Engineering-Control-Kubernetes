"""Asking a model at a provider that accepts chat completions: OpenAI itself, DeepSeek,
Qwen, GLM and Kimi. One request, one answer, no tools: the answer is asked for as a JSON
object, and the platform checks its shape."""

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
) -> Wire:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        limit_name: max_tokens,
        "stream": False,
        # OpenAI holds the model to the shape itself; the others to a JSON object.
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "answer", "strict": True, "schema": schema},
        }
        if strict
        else {"type": "json_object"},
    }
    if strict and effort in EFFORTS:
        body["reasoning_effort"] = effort
    response = await _send(
        http, name, "POST", f"{base_url}/chat/completions", key, seconds, json=body
    )
    try:
        answer = response.json()
        choice = answer["choices"][0]
        message = choice["message"]
    except (ValueError, LookupError, TypeError) as error:
        raise WireError("busy", f"{name} answered something that is not an answer.") from error
    reason = str(choice.get("finish_reason") or "")
    if reason == "length":
        raise WireError("cut", f"The answer was longer than the {max_tokens:,} tokens it may have.")
    if reason == "content_filter" or message.get("refusal"):
        raise WireError(
            "declined", f"The model declined to answer: {said(message.get('refusal'))}".rstrip(": ")
        )
    usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
    details = usage.get("prompt_tokens_details")
    cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else 0
    cached = cached or _count(usage.get("prompt_cache_hit_tokens"))
    return Wire(
        text=str(message.get("content") or ""),
        model=str(answer.get("model") or model),
        read=max(0, _count(usage.get("prompt_tokens")) - cached),
        written=_count(usage.get("completion_tokens")),
        cached=cached,
    )


async def models(
    *, http: httpx.AsyncClient, name: str, key: str, base_url: str, seconds: float = 30
) -> list[str]:
    """The models the key can ask, as the provider lists them."""
    response = await _send(http, name, "GET", f"{base_url}/models", key, seconds)
    try:
        listed = response.json().get("data") or []
    except (ValueError, AttributeError) as error:
        raise WireError("busy", f"{name} answered something that is not a list.") from error
    return sorted(
        {str(entry.get("id")) for entry in listed if isinstance(entry, dict) and entry.get("id")}
    )[:200]
