"""Register the Engineering Control command surface with Hermes.

The gateway hook handles authenticated execution because command handlers do not
receive the platform user identity. This fallback deliberately performs no action.
"""

from __future__ import annotations

COMMANDS = {
    "audit": "Start a bounded read-only audit campaign.",
    "campaigns": "List audit campaigns.",
    "findings": "List verified findings.",
    "run": "Start delivery for one confirmed finding.",
    "status": "Show a workflow run.",
    "evidence": "Show redacted evidence metadata.",
    "approve": "Inspect or approve an immutable deployment candidate.",
    "reject": "Reject an immutable deployment candidate.",
    "pause": "Pause a campaign at its next checkpoint.",
    "resume": "Resume a paused campaign.",
    "cancel": "Cancel a bounded workflow.",
    "usage": "Show provider capacity without invented values.",
    "validate": "Start a bounded synthetic control-plane lifecycle canary.",
    "rollback": "Prepare or start deterministic recovery.",
}


def _hook_required(_raw_args: str) -> str:
    return "Engineering Control gateway hook unavailable; use engineering-ctl locally."


def register(ctx) -> None:
    for name, description in COMMANDS.items():
        ctx.register_command(name, handler=_hook_required, description=description)
