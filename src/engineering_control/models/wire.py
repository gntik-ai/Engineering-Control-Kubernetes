"""What comes back from a provider, whichever way it is spoken to."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# The current answer text, including an empty string when a new attempt starts.
TextUpdate = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class Wire:
    """One answer as the provider gave it."""

    text: str
    # The model that answered: the one asked for, or the one the provider fell back to.
    model: str
    read: int  # tokens read new
    written: int  # tokens written
    cached: int  # tokens read again from the provider's cache
    # Of the tokens read new, the ones the provider wrote to its cache on the way:
    # some providers charge more for these than for a token only read.
    cache_written: int = 0
    provider_ref: str | None = None
    reported_cost: float | None = None
    upstream: str | None = None


class WireError(Exception):
    """A call that brought no answer. ``kind`` says what to do about it:

    - ``refused``: the provider does not accept the key;
    - ``busy``: the provider is over its limit or not answering, and may later;
    - ``invalid``: the provider rejects the request itself (an unknown model, say);
    - ``declined``: the model would not answer this;
    - ``cut``: the answer was longer than the task allows;
    - ``late``: asked in a batch, it was not answered before the batch ended.
    """

    def __init__(
        self,
        kind: str,
        detail: str,
        *,
        retry_after: float | None = None,
        metered: Wire | None = None,
        provider_ref: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.retry_after = retry_after
        self.metered = metered
        self.provider_ref = provider_ref


def said(value: object, limit: int = 300) -> str:
    """A provider's own words, on one line and short."""
    return " ".join(str(value or "").split())[:limit]
