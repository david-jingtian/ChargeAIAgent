from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http import HTTPStatus

import httpx
import pytest

from charge_agent.config import Settings
from charge_agent.db import Connection, connect
from charge_agent.engine import Engine
from charge_agent.models import State, StepName, WorkflowInput
from charge_agent.storage import Store
from charge_agent.tool_client import ToolClient, retry_after_seconds
from charge_agent.tool_storage import KeyConflict, ToolStore


def test_tool_deduplicates_concurrent_calls(database: Connection) -> None:
    def charge(_: int) -> str:
        with connect() as conn:
            return str(
                ToolStore(conn).execute("same-key", StepName.CHARGE, WorkflowInput()).effect_id
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(charge, range(16)))
    assert len(set(ids)) == 1
    assert len(ToolStore(database).effects("same-key")) == 1


def test_key_conflict_rejected(database: Connection) -> None:
    tool = ToolStore(database)
    tool.execute("key", StepName.CHARGE, WorkflowInput())
    with pytest.raises(KeyConflict):
        tool.execute("key", StepName.CHARGE, WorkflowInput(amount_cents=1))
    with pytest.raises(KeyConflict):
        tool.execute("key", StepName.NOTIFY, WorkflowInput())
    assert len(tool.effects("key")) == 1


@pytest.mark.parametrize("ambiguous", [False, True])
def test_retry_exhaustion_distinguishes_unknown(database: Connection, ambiguous: bool) -> None:
    store = Store(database)
    run_id = store.create_run(WorkflowInput())

    def handler(request: httpx.Request) -> httpx.Response:
        if ambiguous:
            raise httpx.ReadTimeout("lost response", request=request)
        return httpx.Response(HTTPStatus.TOO_MANY_REQUESTS)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://tool") as client:
        engine = Engine(store, ToolClient(client), Settings(max_failures=1))
        engine.tick()
    run = store.get_run(run_id)
    assert run is not None
    assert run.state == (State.NEEDS_REVIEW if ambiguous else State.FAILED)
    assert run.steps[1].state == State.PENDING
    assert store.next_step() is None


def test_interrupted_attempt_remains_unknown_after_429(database: Connection) -> None:
    store = Store(database)
    run_id = store.create_run(WorkflowInput())
    step = store.next_step()
    assert step is not None
    store.begin(step)  # Represents a worker that died before recording an outcome.
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(HTTPStatus.TOO_MANY_REQUESTS)),
        base_url="http://tool",
    ) as client:
        Engine(store, ToolClient(client), Settings(max_failures=1)).tick()
    run = store.get_run(run_id)
    assert run is not None and run.state == State.NEEDS_REVIEW


def test_permanent_error_not_retried(database: Connection) -> None:
    store = Store(database)
    run_id = store.create_run(WorkflowInput())
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(HTTPStatus.UNPROCESSABLE_ENTITY)),
        base_url="http://tool",
    ) as client:
        Engine(store, ToolClient(client), Settings()).tick()
    run = store.get_run(run_id)
    assert run is not None and run.state == State.FAILED
    assert run.steps[0].attempts == 1


@pytest.mark.parametrize(
    "header,expected", [(None, 0), ("bad", 0), ("3", 3), ("-1", 0), ("nan", 0), ("inf", 0)]
)
def test_retry_after(header: str | None, expected: float) -> None:
    assert retry_after_seconds(header) == expected


def test_retry_after_http_date() -> None:
    future = datetime.now(UTC) + timedelta(seconds=30)
    assert 28 <= retry_after_seconds(format_datetime(future, usegmt=True)) <= 30
