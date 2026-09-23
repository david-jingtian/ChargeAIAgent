import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus

import httpx
from pydantic import ValidationError

from charge_agent.models import StepRecord, ToolResult


class ToolFailure(Exception):
    def __init__(
        self, message: str, *, retryable: bool, ambiguous: bool, retry_after: float = 0
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.retry_after = retry_after


def retry_after_seconds(value: str | None) -> float:
    if value is None:
        return 0
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, seconds) if math.isfinite(seconds) else 0


class ToolClient:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def execute(self, step: StepRecord) -> ToolResult:
        try:
            response = self.client.post(
                f"/tools/{step.name}",
                json=step.input.model_dump(mode="json"),
                headers={"Idempotency-Key": step.idempotency_key},
            )
        except httpx.TransportError as exc:
            raise ToolFailure(str(exc), retryable=True, ambiguous=True) from exc
        if response.is_success:
            try:
                result = ToolResult.model_validate_json(response.content)
                if result.operation != step.name or result.input != step.input:
                    raise ValueError("Tool response does not match the requested operation/input")
                return result
            except (ValidationError, ValueError) as exc:
                raise ToolFailure("Invalid tool response", retryable=True, ambiguous=True) from exc
        throttled = response.status_code == HTTPStatus.TOO_MANY_REQUESTS
        server_error = response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR
        raise ToolFailure(
            f"Tool returned HTTP {response.status_code}",
            retryable=throttled
            or server_error
            or response.status_code == HTTPStatus.REQUEST_TIMEOUT,
            # Our mock explicitly guarantees 429 is before the effect. Treat 5xx conservatively.
            ambiguous=server_error or response.status_code == HTTPStatus.REQUEST_TIMEOUT,
            retry_after=retry_after_seconds(response.headers.get("Retry-After")),
        )
