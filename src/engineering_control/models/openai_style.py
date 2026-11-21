"""Asking a model at a provider that accepts chat completions: OpenAI itself, Google's
Gemini API, DeepSeek, Qwen, GLM and Kimi. One request, one answer, no tools: the answer
is asked for as a JSON object, and the platform checks its shape."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Final

import httpx

from engineering_control.models.wire import TextUpdate, Wire, WireError, said

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
        kind = (
            "unconfirmed"
            if method == "POST" and name.lower() in ("openrouter", "openai")
            else "busy"
        )
        raise WireError(kind, f"{name} did not answer: {type(error).__name__}") from error
    if (
        method == "POST"
        and name.lower() in ("openrouter", "openai")
        and response.status_code >= 500
    ):
        raise WireError(
            "unconfirmed",
            f"{name} returned HTTP {response.status_code}.",
            provider_ref=response.headers.get("x-request-id"),
        )
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
        raise WireError(
            "unconfirmed" if name == "OpenRouter" else "busy",
            f"{name} returned unreadable answer data."
            if name == "OpenRouter"
            else f"{name} answered something that is not an answer.",
        ) from error
    reason = str(choice.get("finish_reason") or "")
    if name != "OpenRouter" and reason == "length":
        raise WireError("cut", f"The answer was longer than the {max_tokens:,} tokens it may have.")
    if name != "OpenRouter" and (reason == "content_filter" or message.get("refusal")):
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
    from engineering_control.models.openrouter import amount

    router = name == "OpenRouter"
    if router and not all(
        type(usage.get(field)) is int and usage[field] >= 0
        for field in ("prompt_tokens", "completion_tokens")
    ):
        raise WireError(
            "unconfirmed", "OpenRouter returned no usable metering.", provider_ref=answer.get("id")
        )
    wire = Wire(
        text=str(message.get("content") or ""),
        model=str(answer.get("model") or model).removeprefix(model_prefix),
        read=max(0, read_in_all - cached),
        written=written,
        cached=cached,
        cache_written=min(kept, max(0, read_in_all - cached)),
        provider_ref=str(answer["id"]) if router and answer.get("id") else None,
        upstream=str(answer["provider"]) if router and answer.get("provider") else None,
        reported_cost=amount(usage.get("cost")) if router else None,
    )
    if reason == "length" or reason == "content_filter" or message.get("refusal"):
        raise WireError(
            "cut" if reason == "length" else "declined",
            "OpenRouter returned an incomplete answer.",
            metered=wire,
        )
    return wire


async def _events(response: httpx.Response) -> AsyncIterator[str]:
    lines: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))
        elif not line and lines:
            yield "\n".join(lines)
            lines.clear()
    if lines:
        yield "\n".join(lines)


async def _stream(
    http: httpx.AsyncClient,
    name: str,
    url: str,
    key: str,
    seconds: float,
    body: dict[str, Any],
    on_text: TextUpdate,
) -> dict[str, Any]:
    text, refusal, finish = "", "", ""
    usage: dict[str, Any] = {}
    model = body["model"]
    metadata: dict[str, Any] = {}
    await on_text("")
    try:
        async with http.stream(
            "POST",
            url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=seconds,
            json={**body, "stream": True, "stream_options": {"include_usage": True}},
        ) as response:
            if response.status_code != 200:
                await response.aread()
                if name == "OpenRouter" and response.status_code >= 500:
                    raise WireError(
                        "unconfirmed", "OpenRouter interrupted the stream before metering."
                    )
                raise _failure(name, response)
            # Some compatible endpoints answer with a complete JSON object in the same request.
            if "application/json" in response.headers.get("content-type", ""):
                await response.aread()
                return dict(response.json())
            async for event in _events(response):
                if event == "[DONE]":
                    break
                chunk = json.loads(event)
                if chunk.get("error"):
                    raise WireError(
                        "unconfirmed" if name == "OpenRouter" else "busy",
                        f"{name} interrupted the answer.",
                    )
                metadata.update({k: chunk[k] for k in ("id", "provider") if chunk.get(k)})
                model = chunk.get("model") or model
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta") or {}
                    finish = choice.get("finish_reason") or finish
                    refusal += delta.get("refusal") or ""
                    if delta.get("content"):
                        text += delta["content"]
                        await on_text(text)
    except httpx.HTTPError as error:
        raise WireError(
            "unconfirmed" if name == "OpenRouter" else "busy",
            f"{name} did not answer: {type(error).__name__}",
        ) from error
    except (ValueError, TypeError, AttributeError) as error:
        raise WireError(
            "unconfirmed" if name == "OpenRouter" else "busy",
            f"{name} returned unreadable answer data.",
        ) from error
    if not finish:
        raise WireError(
            "unconfirmed" if name == "OpenRouter" else "busy",
            f"{name} stopped sending the answer before it finished.",
        )
    return {
        "model": model,
        "usage": usage,
        **metadata,
        "choices": [{"finish_reason": finish, "message": {"content": text, "refusal": refusal}}],
    }


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
    retry: bool = True,
    routing: dict[str, Any] | None = None,
    on_text: TextUpdate | None = None,
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
    if routing is not None:
        body["provider"] = routing
    url = f"{base_url}/chat/completions"

    async def send(payload: dict[str, Any]) -> Any:
        if on_text is not None:
            return await _stream(http, name, url, key, seconds, payload, on_text)
        response = await _send(http, name, "POST", url, key, seconds, json=payload)
        try:
            return response.json()
        except ValueError as error:
            raise WireError(
                "unconfirmed" if routing is not None else "busy",
                f"{name} answered something that is not an answer.",
            ) from error

    try:
        answer = await send(body)
    except WireError as error:
        if not (retry and strict and routing is None and error.kind == "invalid"):
            raise
        answer = await send({**body, "response_format": {"type": "json_object"}})
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
