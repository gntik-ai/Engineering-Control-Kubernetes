import json

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
