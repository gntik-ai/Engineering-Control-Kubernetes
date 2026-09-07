"""Scoped worker-to-controller publication admission; no credential migration."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import ValidationError

from engineering_control.adapters.github.client import GhCliBroker
from engineering_control.config import Settings
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.models.publication_authority import (
    PublicationAlreadyComplete,
    PublicationClaimed,
    PublicationEffectClaim,
    PublicationExecution,
    PublicationReceiptAcknowledgement,
)

T = TypeVar("T")


class PublicationAuthorityError(ValueError):
    def __init__(self, message: str, *, unknown: bool = False) -> None:
        super().__init__(message)
        self.unknown = unknown


class PublicationAuthorityClient:
    def __init__(
        self, settings: Settings, broker: GhCliBroker, *, client: httpx.Client | None = None
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.client = client
        if settings.worker_kind not in {"delivery", "github"}:
            raise PublicationAuthorityError("publication requires a delivery worker identity")
        self.subject = f"{settings.worker_kind}-runner"
        parsed = urlsplit(settings.evidence_ingest_url or "")
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as error:
            raise PublicationAuthorityError("publication authority is not configured") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not (address.is_private or address.is_loopback)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/v1/evidence"
        ):
            raise PublicationAuthorityError(
                "publication authority origin is not the private ingest"
            )
        self.origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    def _request(self, endpoint: str, value: dict[str, Any]) -> dict[str, Any]:
        try:
            headers = {
                "Authorization": "Bearer " + self.settings.read_evidence_ingest_token(),
                "X-Engineering-Actor": self.subject,
            }
            if self.client is None:
                with httpx.Client(timeout=12, trust_env=False, follow_redirects=False) as client:
                    response = client.post(self.origin + endpoint, json=value, headers=headers)
            else:
                response = self.client.post(self.origin + endpoint, json=value, headers=headers)
            if len(response.content) > 8192:
                raise PublicationAuthorityError(
                    "publication authority response exceeds bound", unknown=True
                )
            data = response.json()
            if not isinstance(data, dict):
                raise PublicationAuthorityError(
                    "publication authority response is invalid", unknown=True
                )
            if response.status_code != 200:
                unresolved = data.get("detail") == {"code": "PUBLICATION_EFFECT_UNRESOLVED"}
                ambiguous = response.status_code not in {401, 403, 409, 413, 422}
                raise PublicationAuthorityError(
                    "publication authority denied or is unavailable",
                    unknown=unresolved or ambiguous,
                )
            return data
        except (httpx.HTTPError, json.JSONDecodeError, OSError) as error:
            raise PublicationAuthorityError(
                "publication authority is unavailable", unknown=True
            ) from error

    def execute(
        self,
        *,
        payload: dict[str, Any],
        plan: dict[str, Any],
        repository: str,
        action: str,
        operation: Callable[[], T],
    ) -> T | None:
        intent = payload.get("publication_intent")
        context = payload.get("orchestration")
        if not isinstance(intent, dict) or not isinstance(context, dict):
            raise PublicationAuthorityError("publication intent or runtime identity is missing")
        execution = PublicationExecution.model_validate(
            {key: context[key] for key in PublicationExecution.model_fields if key in context}
        )
        if intent.get("plan_sha256") != canonical_sha256(plan):
            raise PublicationAuthorityError("publication plan changed after preparation")
        observation = self.broker.observe_issue_revision(
            str(intent["issue_repository"]), int(intent["issue_number"])
        )
        request = PublicationEffectClaim.model_validate(
            {
                "intent_id": intent["intent_id"],
                "intent_binding": intent["intent_binding"],
                "plan_sha256": canonical_sha256(plan),
                "repository": repository,
                "action": action,
                "execution": execution.model_dump(mode="json"),
                "observation": observation.model_dump(mode="json"),
            }
        )
        started = time.monotonic()
        grant = self._request("/v1/publications/claim", request.model_dump(mode="json"))
        expected_effect = uuid5(
            NAMESPACE_URL, f"publication-effect:{request.intent_id}:{repository}:{action}"
        )
        try:
            if grant.get("status") == "ALREADY_COMPLETE":
                complete = PublicationAlreadyComplete.model_validate(grant)
                if complete.effect_id != expected_effect:
                    raise PublicationAuthorityError(
                        "publication result target changed", unknown=True
                    )
                return None
            admitted = PublicationClaimed.model_validate(grant)
            if admitted.effect_id != expected_effect:
                raise PublicationAuthorityError(
                    "publication admission target changed", unknown=True
                )
        except ValidationError as error:
            raise PublicationAuthorityError(
                "publication admission response is unverified", unknown=True
            ) from error
        effect_id, claim_id = admitted.effect_id, admitted.claim_id
        try:
            if datetime.now(UTC) >= admitted.start_before or time.monotonic() - started >= 15:
                raise PublicationAuthorityError(
                    "publication admission expired before effect", unknown=True
                )
            result = operation()
        except BaseException as error:
            try:
                self._request(
                    "/v1/publications/receipt",
                    dict(
                        effect_id=str(effect_id),
                        claim_id=str(claim_id),
                        outcome="UNKNOWN",
                        result_sha256=hashlib.sha256(b"unknown-command-outcome").hexdigest(),
                    ),
                )
            except (PublicationAuthorityError, OSError):
                pass  # Durable STARTED remains blocking when the receipt cannot be saved.
            if isinstance(error, Exception):
                raise PublicationAuthorityError(
                    "publication command outcome is unverified", unknown=True
                ) from error
            # Preserve interruption/cancellation semantics after best-effort
            # receipt; neither state permits a new owner or blind repeat.
            raise
        # No raw output is transmitted or retained here. The typed operation still
        # performs its existing exact-head/PR verification after this receipt.
        digest = hashlib.sha256(repr(result).encode()).hexdigest()
        try:
            receipt = self._request(
                "/v1/publications/receipt",
                dict(
                    effect_id=str(effect_id),
                    claim_id=str(claim_id),
                    outcome="COMPLETE",
                    result_sha256=digest,
                ),
            )
            acknowledgement = PublicationReceiptAcknowledgement.model_validate(receipt)
            if acknowledgement.status != "COMPLETE" or acknowledgement.effect_id != effect_id:
                raise PublicationAuthorityError("publication receipt is not durable", unknown=True)
        except (PublicationAuthorityError, ValidationError) as error:
            raise PublicationAuthorityError(
                "publication effect acknowledgement is unverified", unknown=True
            ) from error
        return result
