from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engineering_control.validation.lifecycle import AcceptanceActivities

__all__ = ["AcceptanceActivities"]


def __getattr__(name: str) -> Any:
    if name != "AcceptanceActivities":
        raise AttributeError(name)
    from engineering_control.validation.lifecycle import AcceptanceActivities

    return AcceptanceActivities
