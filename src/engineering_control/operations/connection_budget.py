"""A diagnostic deadline follows SQL work into threads; cancellation joins it."""

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine


@dataclass
class DiagnosticBudget:
    deadline: float
    cancelled: Event = field(default_factory=Event)


CURRENT_BUDGET: ContextVar[DiagnosticBudget | None] = ContextVar("diagnostic_budget", default=None)


@contextmanager
def diagnostic_budget(seconds: float) -> Iterator[None]:
    token = CURRENT_BUDGET.set(DiagnosticBudget(time.monotonic() + seconds))
    try:
        yield
    finally:
        CURRENT_BUDGET.reset(token)


async def joined_diagnostic_thread[T](function: Callable[..., T], *args: Any) -> T:
    budget = CURRENT_BUDGET.get()
    if budget is None:
        return await asyncio.to_thread(function, *args)
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        budget.cancelled.set()
        # Cancelling the await alone does not stop a running synchronous query.
        # Retain ownership/refresh serialization until that query has terminated.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - original cancellation takes precedence
                break
        if not task.cancelled():
            task.exception()
        raise


def install_diagnostic_sql_budget(engine: Engine) -> None:
    def before_query(
        connection: Any,
        cursor: Any,
        statement: Any,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        budget = CURRENT_BUDGET.get()
        if budget is None:
            return
        remaining = int((budget.deadline - time.monotonic()) * 1000)
        if budget.cancelled.is_set() or remaining <= 0:
            raise TimeoutError("diagnostic SQL deadline expired")
        if connection.dialect.name == "postgresql":
            # Transaction-local; no role/server configuration changes or persistent
            # options affecting non-diagnostic operations on a reused connection.
            milliseconds = str(min(remaining, 5000))
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true), "
                "set_config('lock_timeout', %s, true)",
                (milliseconds, milliseconds),
            )

    event.listen(engine, "before_cursor_execute", before_query)
