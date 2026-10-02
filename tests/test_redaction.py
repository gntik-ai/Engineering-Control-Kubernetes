import json

import pytest

from engineering_control.security.redaction import REDACTED, SecretRedactor


def test_redacts_structured_sensitive_keys_and_tokens() -> None:
    redactor = SecretRedactor()
    fake_github_token = bytes((103, 104, 112, 95)).decode() + "A" * 36
    report = redactor.redact(
        {
            "password": "not-a-real-password",
            "nested": {"message": f"Authorization: Bearer {fake_github_token}"},
            "safe": "visible",
        }
    )
    assert report.value["password"] == REDACTED
    assert fake_github_token not in json.dumps(report.value)
    assert report.value["safe"] == "visible"
    assert report.replacements >= 2


def test_redacts_private_key_multiline() -> None:
    fake_key = "".join(
        ("-----BEGIN ", "PRIVATE KEY-----\n", "ZmFrZQ==\n", "-----END PRIVATE KEY-----")
    )
    report = SecretRedactor().redact_text(fake_key)
    assert report.value == REDACTED
    assert "private_key" in report.categories


def test_preserves_secret_reference_paths_but_not_values() -> None:
    report = SecretRedactor().redact(
        {
            "credential_file": "/protected/persona.env",
            "kubeconfig_path": "/protected/release.kubeconfig",
            "credential": "value-that-must-not-persist",
            "kubeconfig": "embedded-content-that-must-not-persist",
        }
    )
    assert report.value["credential_file"] == "/protected/persona.env"
    assert report.value["kubeconfig_path"] == "/protected/release.kubeconfig"
    assert report.value["credential"] == REDACTED
    assert report.value["kubeconfig"] == REDACTED


def test_preserves_numeric_usage_counters_but_redacts_token_strings() -> None:
    report = SecretRedactor().redact(
        {
            "usage": {
                "input_tokens": 123,
                "output_tokens": 45,
                "cache_read_input_tokens": 67,
                "cache_creation_input_tokens": 8,
            },
            "input_tokens": "credential-like-string",
            "auth_token": 123456,
        }
    )

    assert report.value["usage"] == {
        "input_tokens": 123,
        "output_tokens": 45,
        "cache_read_input_tokens": 67,
        "cache_creation_input_tokens": 8,
    }
    assert report.value["input_tokens"] == REDACTED
    assert report.value["auth_token"] == REDACTED


def test_preserves_only_typed_session_revocation_cleanup_aggregates() -> None:
    report = SecretRedactor().redact(
        {
            "dashboard_session_revocation_cleanup_count": 28,
            "deleted_dashboard_session_revocation_count": 10,
            "remaining_dashboard_session_revocation_count": 18,
            "dashboard_session_revocation_cleanup_truncated": True,
            "nested": {
                "dashboard_session_revocation_cleanup_count": "28",
                "deleted_dashboard_session_revocation_count": -1,
                "remaining_dashboard_session_revocation_count": "secret-value",
                "dashboard_session_revocation_cleanup_truncated": "true",
            },
        }
    )

    assert report.value["dashboard_session_revocation_cleanup_count"] == 28
    assert report.value["deleted_dashboard_session_revocation_count"] == 10
    assert report.value["remaining_dashboard_session_revocation_count"] == 18
    assert report.value["dashboard_session_revocation_cleanup_truncated"] is True
    assert report.value["nested"] == {
        "dashboard_session_revocation_cleanup_count": REDACTED,
        "deleted_dashboard_session_revocation_count": REDACTED,
        "remaining_dashboard_session_revocation_count": REDACTED,
        "dashboard_session_revocation_cleanup_truncated": REDACTED,
    }


@pytest.mark.parametrize(
    "value",
    [
        "password=" + bytes(range(65, 91)).decode(),
        "token=" + "x" * 30,
        "Authorization: Bearer " + "y" * 32,
        "Cookie: dashboard_session=" + "z" * 32,
        "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12,
        "https://example.invalid/path?access_token=" + "d" * 32 + "&safe=yes",
        "token: " + "e" * 32 + "\nclient-key-data: " + "f" * 64,
        "\x1b[31mAuthorization: Bearer " + "g" * 32 + "\x1b[0m",
        "sk-" + "h" * 32,
    ],
)
def test_adversarial_visible_output_is_redacted(value: str) -> None:
    report = SecretRedactor().redact_text(value)
    assert REDACTED in report.value
    assert value not in report.value
    assert report.replacements >= 1


def test_nested_json_secret_is_redacted_without_original_retention() -> None:
    original = "nested-value-" + "i" * 24
    report = SecretRedactor().redact_json_line(
        json.dumps({"safe": "visible", "nested": {"client_secret": original}})
    )
    assert original not in report.value
    assert REDACTED in report.value


def test_git_commit_sha_remains_visible() -> None:
    sha = "a1" * 20
    report = SecretRedactor().redact_text(f"commit {sha} completed")
    assert sha in report.value
    assert report.replacements == 0
