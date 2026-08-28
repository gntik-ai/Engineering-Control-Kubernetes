"""Typed, policy-gated operations shared by dashboard, Telegram, and CLI."""

from engineering_control.operations.models import Command, parse_command

__all__ = ["Command", "parse_command"]
