from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class StepName(StrEnum):
    CHARGE = "charge"
    PROVISION = "provision"
    NOTIFY = "notify"


WORKFLOW_VERSION = "purchase-v1"
WORKFLOW = tuple(StepName)


class State(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class WorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    customer_id: str = Field(default="demo-customer", min_length=1, max_length=100)
    amount_cents: int = Field(default=2500, gt=0, strict=True)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class ToolResult(BaseModel):
    effect_id: UUID
    operation: StepName
    input: WorkflowInput


class StepRecord(BaseModel):
    run_id: UUID
    position: int
    name: StepName
    state: State
    idempotency_key: str
    input: WorkflowInput
    attempts: int
    failures: int
    next_attempt_at: datetime | None
    result: ToolResult | None
    error: str | None
    outcome_unknown: bool


class RunRecord(BaseModel):
    id: UUID
    workflow_version: str
    state: State
    input: WorkflowInput
    created_at: datetime
    steps: list[StepRecord]


class EffectRecord(BaseModel):
    idempotency_key: str
    result: ToolResult
    created_at: datetime


class Health(BaseModel):
    status: str = "ok"


class Event(BaseModel):
    event: str
    fields: dict[str, JsonValue]
