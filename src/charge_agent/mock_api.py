import logging
import time
from http import HTTPStatus
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException

from charge_agent.config import settings
from charge_agent.db import connect
from charge_agent.models import EffectRecord, Health, StepName, ToolResult, WorkflowInput
from charge_agent.tool_storage import KeyConflict, ToolStore, failure_roll

app = FastAPI(title="Durable mock tool")
logger = logging.getLogger("uvicorn.error")


@app.get("/health", response_model=Health)
def health() -> Health:
    with connect() as conn:
        conn.execute("SELECT 1 FROM mock_tool.effects LIMIT 1")
    return Health()


@app.post("/tools/{operation}", response_model=ToolResult)
def execute(
    operation: StepName,
    payload: WorkflowInput,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
) -> ToolResult:
    try:
        with connect() as conn:
            store = ToolStore(conn)
            count = store.request_count(idempotency_key)
            existing = store.existing(idempotency_key, operation, payload)
            if existing is not None:
                return existing
            roll = failure_roll(settings.tool_seed, idempotency_key, count)
            if count <= settings.tool_fail_first or roll < settings.tool_failure_rate:
                status = (
                    HTTPStatus.TOO_MANY_REQUESTS
                    if count <= settings.tool_fail_first or count % 2
                    else HTTPStatus.SERVICE_UNAVAILABLE
                )
                raise HTTPException(
                    status_code=status,
                    detail="Injected transient failure before side effect",
                    headers={"Retry-After": str(settings.tool_retry_after_seconds)},
                )
            result = store.execute(idempotency_key, operation, payload)
    except KeyConflict as exc:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(exc)) from exc
    # No DB transaction is held during the delay. This creates a lost-response test window.
    logger.info("effect_committed key=%s effect=%s", idempotency_key, result.effect_id)
    if settings.tool_response_delay_seconds:
        time.sleep(settings.tool_response_delay_seconds)
    return result


@app.get("/effects", response_model=list[EffectRecord])
def effects(prefix: str = "") -> list[EffectRecord]:
    with connect() as conn:
        return ToolStore(conn).effects(prefix)
