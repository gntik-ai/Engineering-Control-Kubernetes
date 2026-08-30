"""Register the Engineering Control command surface with Hermes.

The gateway hook handles authenticated execution because command handlers do not
receive the platform user identity. This fallback deliberately performs no action.
"""

from __future__ import annotations

COMMANDS = {
    "audit-plan": "Show a validated paired-repository campaign plan.",
    "audit": "Start a bounded read-only audit campaign.",
    "bootstrap-plan": "Plan the first deterministic LLMWiki staging bootstrap.",
    "bootstrap": "Execute an approval-bound LLMWiki staging bootstrap.",
    "campaigns": "List audit campaigns.",
    "findings": "List verified findings.",
    "loop": "Plan or control a durable issue-solution loop.",
    "enhancements": "Discover, review, or run an approved enhancement.",
    "verify": "Start independent verification for one candidate finding.",
    "run": "Start delivery for one confirmed finding.",
    "status": "Show a workflow run.",
    "evidence": "Show redacted evidence metadata.",
    "export-campaign": "Create a bounded redacted campaign summary export.",
    "export-workflow": "Create a bounded redacted workflow summary export.",
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
    try:
        from hermes_cli.commands import resolve_command
    except ImportError:  # Unit validation runs outside the Hermes vendor environment.

        def resolve_command(_name: str) -> None:
            return None

    for name, description in COMMANDS.items():
        if resolve_command(name) is None:
            ctx.register_command(name, handler=_hook_required, description=description)
