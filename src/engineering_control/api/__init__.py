from typing import Any

from fastapi import FastAPI


def create_app(**kwargs: Any) -> FastAPI:
    """Import lazily so database repository users do not form an API service cycle."""
    from engineering_control.api.main import create_app as create_control_app

    return create_control_app(**kwargs)


__all__ = ["create_app"]
