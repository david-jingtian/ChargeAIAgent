import signal
from uuid import UUID

import httpx
import pytest
from conftest import Processes, wait_until

from charge_agent.db import Connection
from charge_agent.models import State, WorkflowInput
from charge_agent.storage import Store
from charge_agent.tool_storage import ToolStore


def finished(store: Store, run_id: UUID) -> bool:
    run = store.get_run(run_id)
    assert run is not None
    return run.state == State.COMPLETED


@pytest.mark.parametrize("point", ["before_tool", "after_tool", "during_commit", "after_commit"])
def test_sigkill_at_checkpoint_boundaries(
    database: Connection, processes: Processes, point: str
) -> None:
    url = processes.service("charge_agent.mock_api:app")
    store = Store(database)
    run_id = store.create_run(WorkflowInput())
    worker, log = processes.worker(url, point)
    wait_until(lambda: '"event": "paused"' in log.read_text())
    worker.kill()  # Real SIGKILL: no finally blocks or graceful shutdown.
    assert worker.wait(timeout=5) == -signal.SIGKILL
    processes.worker(url)
    wait_until(lambda: finished(store, run_id))
    effects = ToolStore(database).effects(str(run_id))
    assert len(effects) == 3
    assert len({effect.result.operation for effect in effects}) == 3
    run = store.get_run(run_id)
    assert run is not None
    assert run.steps[0].attempts == (1 if point == "after_commit" else 2)
    assert all(step.state == State.COMPLETED for step in run.steps)
    if point != "before_tool":
        charge_count = database.execute(
            "SELECT count FROM mock_tool.requests WHERE idempotency_key = %s",
            (run.steps[0].idempotency_key,),
        ).fetchone()
        assert charge_count is not None
        assert charge_count["count"] == (1 if point == "after_commit" else 2)


def test_sigkill_after_effect_before_response(database: Connection, processes: Processes) -> None:
    url = processes.service(
        "charge_agent.mock_api:app", {"CHARGE_TOOL_RESPONSE_DELAY_SECONDS": "1"}
    )
    store = Store(database)
    run_id = store.create_run(WorkflowInput())
    worker, _ = processes.worker(url)
    wait_until(lambda: len(ToolStore(database).effects(str(run_id))) == 1)
    worker.kill()
    worker.wait(timeout=5)
    processes.worker(url)
    wait_until(lambda: finished(store, run_id))
    assert len(ToolStore(database).effects(str(run_id))) == 3
    run = store.get_run(run_id)
    assert run is not None
    assert run.steps[0].attempts == 2


def test_restart_preserves_retry_wait(database: Connection, processes: Processes) -> None:
    url = processes.service(
        "charge_agent.mock_api:app",
        {"CHARGE_TOOL_FAIL_FIRST": "1", "CHARGE_TOOL_RETRY_AFTER_SECONDS": "2"},
    )
    store = Store(database)
    run_id = store.create_run(WorkflowInput())
    worker, _ = processes.worker(url)

    def waiting() -> bool:
        run = store.get_run(run_id)
        return run is not None and run.steps[0].state == State.RETRY_WAIT

    wait_until(waiting)
    before = store.get_run(run_id)
    assert before is not None
    due = before.steps[0].next_attempt_at
    worker.kill()
    worker.wait(timeout=5)
    processes.worker(url)
    wait_until(lambda: finished(store, run_id))
    effects = ToolStore(database).effects(str(run_id))
    assert due is not None and effects[0].created_at >= due
    run = store.get_run(run_id)
    assert run is not None
    assert [step.attempts for step in run.steps] == [2, 2, 2]
    assert [step.failures for step in run.steps] == [1, 1, 1]


def test_second_worker_exits(database: Connection, processes: Processes) -> None:
    first, log = processes.worker("http://127.0.0.1:1")
    wait_until(lambda: "worker_ready" in log.read_text())
    second, second_log = processes.worker("http://127.0.0.1:1")
    assert second.wait(timeout=5) != 0
    assert "Another worker" in second_log.read_text()
    assert first.poll() is None


def test_api_runs_workflow(database: Connection, processes: Processes) -> None:
    tool_url = processes.service("charge_agent.mock_api:app")
    api_url = processes.service("charge_agent.api:app")
    processes.worker(tool_url)
    response = httpx.post(f"{api_url}/runs", json={"amount_cents": 100})
    assert response.status_code == 202
    run_id = UUID(response.json()["id"])
    wait_until(lambda: finished(Store(database), run_id))
    response = httpx.get(f"{api_url}/runs/{run_id}")
    assert response.json()["state"] == "completed"
    assert httpx.post(f"{api_url}/runs", json={"amount_cents": -1}).status_code == 422
