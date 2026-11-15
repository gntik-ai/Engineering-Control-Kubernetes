"""Asking a Claude model over Anthropic's API, through Anthropic's own library.

The answer's shape is given to the API itself (structured outputs), so the model can
only answer in it; the platform checks the answer all the same. Requests are streamed,
so a long answer does not run into a time limit on the way. On the models that can
decline a request for safety reasons, the API's own fallback is asked for: the same
request is then answered by the model Anthropic names for that case.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

import anthropic
import httpx2

from engineering_control.models.wire import Wire, WireError, said

# The models whose requests can be declined by a safety classifier and that the API can
# answer in place of: the fallback is asked for on these.
FALLBACK_MODELS: Final = (
    "claude-fable-5-2",
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
)
FALLBACK_BETA: Final = "server-side-fallback-2026-07-01"
EFFORTS: Final = ("low", "medium", "high", "xhigh", "max")
# How often the library itself asks again when Anthropic is busy, before the platform's
# own waiting takes over.
RETRIES = 2
HttpFactory = Callable[[], httpx2.AsyncClient]


def _client(
    key: str, base_url: str, seconds: float, http: HttpFactory | None
) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=key,
        base_url=base_url,
        timeout=seconds,
        max_retries=RETRIES,
        http_client=http() if http is not None else None,
    )


def _failure(error: anthropic.APIError) -> WireError:
    if isinstance(error, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return WireError("refused", f"Anthropic does not accept the key: {said(error.message)}")
    if isinstance(error, anthropic.RateLimitError):
        try:
            wait: float | None = float(error.response.headers.get("retry-after") or "")
        except ValueError:
            wait = None
        return WireError(
            "busy", f"Anthropic's limit is reached: {said(error.message)}", retry_after=wait
        )
    if isinstance(error, anthropic.APIStatusError):
        if error.status_code >= 500:
            return WireError(
                "busy", f"Anthropic answered {error.status_code}: {said(error.message)}"
            )
        return WireError(
            "invalid",
            f"Anthropic rejected the request ({error.status_code}): {said(error.message)}",
        )
    # No answer at all: the connection, or the time allowed.
    return WireError(
        "busy", f"Anthropic did not answer: {said(error.message) or type(error).__name__}"
    )


async def complete(
    *,
    key: str,
    base_url: str,
    model: str,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    max_tokens: int,
    seconds: float,
    effort: str | None = None,
    http: HttpFactory | None = None,
) -> Wire:
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
    if effort in EFFORTS:
        output_config["effort"] = effort
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": output_config,
    }
    client = _client(key, base_url, seconds, http)
    try:
        if model in FALLBACK_MODELS:
            async with client.beta.messages.stream(
                **request, betas=[FALLBACK_BETA], fallbacks="default"
            ) as stream:
                message: Any = await stream.get_final_message()
        else:
            async with client.messages.stream(**request) as stream:
                message = await stream.get_final_message()
    except anthropic.APIError as error:
        raise _failure(error) from error
    finally:
        await client.close()
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        why = said(getattr(details, "explanation", "") or getattr(details, "category", ""))
        raise WireError("declined", f"The model declined to answer{': ' + why if why else '.'}")
    if message.stop_reason == "max_tokens":
        raise WireError("cut", f"The answer was longer than the {max_tokens:,} tokens it may have.")
    text = "".join(block.text for block in message.content if block.type == "text")
    usage = message.usage
    return Wire(
        text=text,
        model=str(message.model or model),
        read=int(usage.input_tokens or 0) + int(usage.cache_creation_input_tokens or 0),
        written=int(usage.output_tokens or 0),
        cached=int(usage.cache_read_input_tokens or 0),
    )


async def models(
    *, key: str, base_url: str, seconds: float = 30, http: HttpFactory | None = None
) -> list[str]:
    """The models the key can ask, as the provider lists them."""
    client = _client(key, base_url, seconds, http)
    found: list[str] = []
    try:
        async for model in client.models.list(limit=100):
            found.append(str(model.id))
            if len(found) >= 200:
                break
    except anthropic.APIError as error:
        raise _failure(error) from error
    finally:
        await client.close()
    return sorted(found)
