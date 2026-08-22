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
