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


def content(
    prompt: str, cuts: tuple[int, ...] = (), keep: tuple[int, ...] = (), hour: bool = False
) -> str | list[dict[str, Any]]:
    """The prompt as the API takes it. With nothing to keep it is one text. Otherwise it
    is cut into pieces at ``cuts``, and the pieces that end at an offset of ``keep`` are
    marked: what comes before a mark is written to Anthropic's cache, and read from it
    by a later question that begins with the same pieces."""
    ends = {end for end in keep if 0 < end <= len(prompt)}
    if not ends:
        return prompt
    edges = sorted({cut for cut in (*cuts, *ends) if 0 < cut < len(prompt)})
    blocks: list[dict[str, Any]] = []
    start = 0
    for edge in (*edges, len(prompt)):
        block: dict[str, Any] = {"type": "text", "text": prompt[start:edge]}
        if edge in ends:
            block["cache_control"] = {"type": "ephemeral", **({"ttl": "1h"} if hour else {})}
        blocks.append(block)
        start = edge
    return blocks


def request_of(
    *,
    model: str,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    max_tokens: int,
    effort: str | None = None,
    cuts: tuple[int, ...] = (),
    keep: tuple[int, ...] = (),
    hour: bool = False,
) -> dict[str, Any]:
    """One question as a request to the Messages API, asked at once or in a batch."""
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
    if effort in EFFORTS:
        output_config["effort"] = effort
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content(prompt, cuts, keep, hour)}],
        "output_config": output_config,
    }


def read(message: Any, model: str, max_tokens: int) -> Wire:
    """What a message of the API says, or why it is no answer."""
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        why = said(getattr(details, "explanation", "") or getattr(details, "category", ""))
        raise WireError("declined", f"The model declined to answer{': ' + why if why else '.'}")
    if message.stop_reason == "max_tokens":
        raise WireError("cut", f"The answer was longer than the {max_tokens:,} tokens it may have.")
    text = "".join(block.text for block in message.content if block.type == "text")
    usage = message.usage
    kept = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    return Wire(
        text=text,
        model=str(message.model or model),
        read=int(usage.input_tokens or 0) + kept,
        written=int(usage.output_tokens or 0),
        cached=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_written=kept,
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
    cuts: tuple[int, ...] = (),
    keep: tuple[int, ...] = (),
    hour: bool = False,
) -> Wire:
    request = request_of(
        model=model,
        system=system,
        prompt=prompt,
        schema=schema,
        max_tokens=max_tokens,
        effort=effort,
        cuts=cuts,
        keep=keep,
        hour=hour,
    )
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
    return read(message, model, max_tokens)


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
