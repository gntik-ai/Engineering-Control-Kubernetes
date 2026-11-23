"""Ordering shared by dashboard queries that paginate in SQL or in memory."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

Direction = Literal["asc", "desc"]


def sql_order(value: Any, direction: Direction) -> Any:
    return (value.desc() if direction == "desc" else value.asc()).nulls_last()


def ordered[T](rows: list[T], key: Callable[[T], Any], direction: Direction) -> list[T]:
    """Stable order with missing values last in either direction."""
    values = [(row, key(row)) for row in rows]
    present = [
        (row, value.casefold() if isinstance(value, str) else value)
        for row, value in values
        if value is not None
    ]
    present.sort(key=lambda pair: pair[1], reverse=direction == "desc")
    return [row for row, _ in present] + [row for row, value in values if value is None]
