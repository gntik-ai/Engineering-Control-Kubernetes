"""NEW-246: a delivery's spec agent sees the whole issue body; maker and checker prompts
keep the 20000-character head and tail they had before."""

from __future__ import annotations

from engineering_control.activities import github as github_activity
from engineering_control.activities import metadata


def _payload(actual: str) -> dict[str, object]:
    return {
        "expected_behavior": "The page matches the legacy behaviour.",
        "actual_behavior": actual,
        "minimal_reproduction": ["open /console/workspace-secrets"],
    }


def _delivery_body() -> str:
    # A bilingual UI-migration body like falcone #1262 (42 753 chars).
    body = "## Decisiones del operador\n" + "Requisito: Área de trabajo, Sí.\n" * 1_400
    assert github_activity.ISSUE_BODY_REFRESH_LIMIT < len(body)
    assert len(body) <= github_activity.DELIVERY_ISSUE_BODY_LIMIT
    return body


def test_spec_issue_content_keeps_the_whole_delivery_body() -> None:
    body = _delivery_body()
    excerpt, truncated = github_activity.bounded_issue_body(
        body,
        github_activity.DELIVERY_ISSUE_BODY_LIMIT,
        github_activity.DELIVERY_ISSUE_BODY_HEAD,
    )
    assert truncated is False

    content = metadata._issue_content(_payload(excerpt))

    assert content is not None and excerpt in content


def test_maker_and_checker_issue_context_is_unchanged_by_the_larger_body() -> None:
    body = _delivery_body()
    bounded, truncated = github_activity.bounded_issue_body(body)
    assert truncated is True

    # The context from the whole delivery body equals the context from the old 20000-char
    # refresh, so maker and checker prompts do not grow (AgentRequest's 200000 limit).
    assert metadata._issue_context(_payload(body)) == metadata._issue_context(_payload(bounded))
