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
    loose: bool = False,
) -> dict[str, Any]:
    """One question as a request to the Messages API, asked at once or in a batch. The
    API is given the shape of the answer and holds the model to it, unless the question
    is ``loose``: then the shape is only said in the question. The API reads a shape it
    is given before everything else, so two questions with different shapes begin
    differently for it and share nothing it kept."""
    output_config: dict[str, Any] = (
        {} if loose else {"format": {"type": "json_schema", "schema": schema}}
    )
    if effort in EFFORTS:
        output_config["effort"] = effort
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content(prompt, cuts, keep, hour)}],
        **({"output_config": output_config} if output_config else {}),
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
    loose: bool = False,
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
        loose=loose,
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


# --- batches ---------------------------------------------------------------------------------
#
# A batch is a list of requests Anthropic answers within a day, most within the hour, at
# half the price. It is made, then asked about until it has ended, then its answers are
# fetched: each by the name the request was given, in no particular order.


async def batch_create(
    *,
    key: str,
    base_url: str,
    requests: list[tuple[str, dict[str, Any]]],
    seconds: float = 120,
    http: HttpFactory | None = None,
) -> str:
    """Hand Anthropic the requests as one batch. Returns the batch's name there."""
    client = _client(key, base_url, seconds, http)
    try:
        made = await client.messages.batches.create(
            requests=[{"custom_id": name, "params": params} for name, params in requests]  # type: ignore[typeddict-item]
        )
    except anthropic.APIError as error:
        raise _failure(error) from error
    finally:
        await client.close()
    return str(made.id)


def _state(batch: Any) -> dict[str, Any]:
    counts = batch.request_counts
    return {
        "id": str(batch.id),
        "ended": batch.processing_status == "ended",
        "status": str(batch.processing_status),
        "created_at": batch.created_at,
        "expires_at": batch.expires_at,
        "waiting": int(counts.processing or 0),
        "succeeded": int(counts.succeeded or 0),
        "errored": int(counts.errored or 0),
        "cancelled": int(counts.canceled or 0),
        "expired": int(counts.expired or 0),
    }


async def batch_state(
    *, key: str, base_url: str, batch_id: str, seconds: float = 60, http: HttpFactory | None = None
) -> dict[str, Any]:
    """Where a batch is: whether it has ended, and how many of its requests ended how."""
    client = _client(key, base_url, seconds, http)
    try:
        return _state(await client.messages.batches.retrieve(batch_id))
    except anthropic.APIError as error:
        raise _failure(error) from error
    finally:
        await client.close()


async def batch_recent(
    *, key: str, base_url: str, seconds: float = 60, http: HttpFactory | None = None
) -> list[dict[str, Any]]:
    """The batches made last with this key, the latest first."""
    client = _client(key, base_url, seconds, http)
    found: list[dict[str, Any]] = []
    try:
        async for batch in client.messages.batches.list(limit=20):
            found.append(_state(batch))
            if len(found) >= 20:
                break
    except anthropic.APIError as error:
        raise _failure(error) from error
    finally:
        await client.close()
    return found


async def batch_cancel(
    *, key: str, base_url: str, batch_id: str, seconds: float = 60, http: HttpFactory | None = None
) -> None:
    """Ask Anthropic to stop a batch. What it has answered by then stays answered."""
    client = _client(key, base_url, seconds, http)
    try:
        await client.messages.batches.cancel(batch_id)
    except anthropic.APIError as error:
        raise _failure(error) from error
    finally:
        await client.close()


async def batch_results(
    *,
    key: str,
    base_url: str,
    batch_id: str,
    tokens: dict[str, int],
    seconds: float = 300,
    http: HttpFactory | None = None,
) -> dict[str, Wire | WireError]:
    """What a batch that has ended answered, by the name of each request: an answer, or
    why there is none. ``tokens`` says how long each answer was allowed to be."""
    client = _client(key, base_url, seconds, http)
    found: dict[str, Wire | WireError] = {}
    try:
        async for entry in await client.messages.batches.results(batch_id):
            name, result = str(entry.custom_id), entry.result
            if result.type == "succeeded":
                try:
                    found[name] = read(result.message, "", tokens.get(name, 0))
                except WireError as error:
                    found[name] = error
            elif result.type == "errored":
                inner = result.error.error
                kind = "invalid" if inner.type == "invalid_request_error" else "busy"
                found[name] = WireError(kind, f"Anthropic could not answer: {said(inner.message)}")
            elif result.type == "expired":
                found[name] = WireError("late", "Anthropic did not answer within its day.")
            else:
                found[name] = WireError("late", "The batch was stopped before this was answered.")
    except anthropic.APIError as error:
        raise _failure(error) from error
    finally:
        await client.close()
    return found


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
