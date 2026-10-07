from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

REDACTED = "[REDACTED]"
# Patterns that replace a remainder must not re-match their own output: the ingest
# service re-redacts pre-redacted evidence and rejects it on any replacement.
_NOT_REDACTED = rf"(?!\s*{re.escape(REDACTED)})"


@dataclass(frozen=True)
class RedactionReport:
    value: Any
    replacements: int
    categories: tuple[str, ...]


class SecretRedactor:
    """Redact credentials before logs, prompts, evidence, or database persistence."""

    _patterns: tuple[tuple[str, re.Pattern[str], str], ...] = (
        (
            "bearer",
            re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
            rf"\1{REDACTED}",
        ),
        (
            "basic_authorization",
            re.compile(r"(?i)\b(basic\s+)[A-Za-z0-9+/=]{12,}"),
            rf"\1{REDACTED}",
        ),
        (
            "cookie",
            re.compile(rf"(?i)\b((?:set-)?cookie\s*:\s*){_NOT_REDACTED}[^\r\n]{{4,}}"),
            rf"\1{REDACTED}",
        ),
        (
            "jwt",
            re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
            REDACTED,
        ),
        (
            "github_token",
            re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
            REDACTED,
        ),
        (
            "provider_token",
            re.compile(r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|hcloud_[A-Za-z0-9_-]{20,})\b"),
            REDACTED,
        ),
        ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),
        (
            "private_key",
            re.compile(
                r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
                r"-----END [A-Z0-9 ]*PRIVATE KEY-----"
            ),
            REDACTED,
        ),
        (
            "credential_assignment",
            re.compile(
                r"(?im)^(\s*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|encryption[_-]?key|telegram[_-]?bot[_-]?token)\s*[:=]\s*)"
                rf"{_NOT_REDACTED}([^\s#]+|['\"][^'\"]+['\"])(\s*(?:#.*)?)$"
            ),
            rf"\1{REDACTED}\3",
        ),
        (
            "url_credentials",
            re.compile(rf"(?i)(https?://[^\s:/@]+:){_NOT_REDACTED}[^\s/@]+(@)"),
            rf"\1{REDACTED}\2",
        ),
        (
            "url_query_credential",
            re.compile(
                r"(?i)([?&](?:access[_-]?token|auth|authorization|key|password|passwd|secret|session|signature|token)=)"
                rf"{_NOT_REDACTED}[^&#\s]+"
            ),
            rf"\1{REDACTED}",
        ),
        (
            "kubeconfig_credential",
            re.compile(
                rf"(?im)^(\s*(?:token|client-key-data|password)\s*:\s*){_NOT_REDACTED}[^\r\n]+$"
            ),
            rf"\1{REDACTED}",
        ),
    )
    _sensitive_keys = re.compile(
        r"(?i)(authorization|cookie|password|passwd|secret|session|token|api.?key|access.?key|client.?secret|private.?key|encryption.?key|credential|kubeconfig)"
    )
    _safe_reference_keys = re.compile(r"(?i)(?:_path|_file|_filename|_ref|_reference|_id|_sha256)$")
    _safe_nonnegative_counter_keys = frozenset(
        {
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "dashboard_session_revocation_cleanup_count",
            "deleted_dashboard_session_revocation_count",
            "remaining_dashboard_session_revocation_count",
        }
    )
    _safe_boolean_flag_keys = frozenset(
        {
            "dashboard_session_revocation_cleanup_truncated",
        }
    )

    def __init__(self) -> None:
        self._runtime_secrets: set[str] = set()

    def register_secret(self, value: str) -> None:
        """Register bounded execution-only material before any output is captured."""
        if value and value != REDACTED:
            self._runtime_secrets.add(value)

    def unregister_secret(self, value: str) -> None:
        self._runtime_secrets.discard(value)

    def redact_text(self, value: str) -> RedactionReport:
        replacements = 0
        categories: list[str] = []
        redacted = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", value)
        for secret in sorted(self._runtime_secrets, key=len, reverse=True):
            redacted, count = redacted.replace(secret, REDACTED), redacted.count(secret)
            if count:
                replacements += count
                categories.append("runtime_secret")
        for category, pattern, replacement in self._patterns:
            redacted, count = pattern.subn(replacement, redacted)
            if count:
                replacements += count
                categories.append(category)
        return RedactionReport(redacted, replacements, tuple(sorted(set(categories))))

    def redact(self, value: Any) -> RedactionReport:
        categories: set[str] = set()
        replacements = 0

        def walk(item: Any, key: str | None = None) -> Any:
            nonlocal replacements
            if (
                key is not None
                and key.lower() in self._safe_nonnegative_counter_keys
                and isinstance(item, int)
                and not isinstance(item, bool)
                and item >= 0
            ):
                return item
            if (
                key is not None
                and key.lower() in self._safe_boolean_flag_keys
                and isinstance(item, bool)
            ):
                return item
            if (
                key is not None
                and self._sensitive_keys.search(key)
                and not self._safe_reference_keys.search(key)
            ):
                if item not in (None, "", REDACTED):
                    replacements += 1
                    categories.add("sensitive_key")
                    return REDACTED
            if isinstance(item, str):
                report = self.redact_text(item)
                replacements += report.replacements
                categories.update(report.categories)
                return report.value
            if isinstance(item, dict):
                return {str(k): walk(v, str(k)) for k, v in item.items()}
            if isinstance(item, list):
                return [walk(v) for v in item]
            if isinstance(item, tuple):
                return tuple(walk(v) for v in item)
            return item

        result = walk(value)
        return RedactionReport(result, replacements, tuple(sorted(categories)))

    def redact_json_line(self, line: str) -> RedactionReport:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            return self.redact_text(line)
        report = self.redact(parsed)
        return RedactionReport(
            json.dumps(report.value, separators=(",", ":"), sort_keys=True),
            report.replacements,
            report.categories,
        )


# NEW-146: the bound of an operator-visible failure detail (after redaction).
BLOCKER_DETAIL_LIMIT = 300


# NEW-146 (reviews 3-7): a diagnostic detail is free text that exception and validation
# messages shape arbitrarily (prefixes, quotes, escapes, punctuation, ANSI codes, newlines),
# so no value grammar is trusted: from the first credential assignment on, the whole rest
# of the detail is redacted. Over-redacting a diagnostic is acceptable; a partial leak is not.
_INLINE_CREDENTIAL = re.compile(
    r"(?is)\b((?:password|passwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret"
    r"|encryption[_-]?key|telegram[_-]?bot[_-]?token|client-key-data)\s*[:=]\s*)"
    # No already-redacted exemption: re-redacting is idempotent, and a sentinel inside the
    # text must not shield what follows it.
    r"\S.*"
)
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _redact_inline_credentials(text: str) -> str:
    return _INLINE_CREDENTIAL.sub(lambda match: f"{match.group(1)}{REDACTED}", text)


def redact_detail_text(text: str, redactor: SecretRedactor | None = None) -> str:
    """A diagnostic's text with terminal escapes removed and every credential assignment
    redacted to the end of the detail, before the redactor's own rules run (a rule that
    ran first could blank only part of a value and hide the rest from this one)."""
    inline = _redact_inline_credentials(_ANSI_ESCAPE.sub("", text))
    return str((redactor or SecretRedactor()).redact_text(inline).value)


def bounded_detail(
    text: str, redactor: SecretRedactor | None = None, *, limit: int = BLOCKER_DETAIL_LIMIT
) -> str:
    """One redacted line of at most ``limit`` characters.

    Redacted before it is cut, so a cut never leaves part of a secret unrecognised. Besides
    the redactor's rules, credential assignments are redacted anywhere in the line, so a
    label or an exception's own prefix in front of ``password=...`` cannot hide it."""
    # NEW-146 reviews 5-6: inline credentials (spanning newlines) are redacted on the raw
    # text before the redactor's rules, and again after flattening to one line.
    redacted = redact_detail_text(text, redactor)
    line = " ".join(redacted.split())
    line = _redact_inline_credentials(line)
    if len(line) > limit:
        line = line[: limit - 3].rstrip() + "..."
    return line
