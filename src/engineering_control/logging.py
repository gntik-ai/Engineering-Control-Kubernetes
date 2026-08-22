from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any, cast

import structlog

from engineering_control.security.redaction import SecretRedactor


def _redact(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return cast(MutableMapping[str, Any], SecretRedactor().redact(dict(event_dict)).value)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=level.upper())
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.JSONRenderer(sort_keys=True),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
