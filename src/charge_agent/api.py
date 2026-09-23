from http import HTTPStatus
from uuid import UUID

from fastapi import FastAPI, HTTPException

from charge_agent.db import connect
from charge_agent.models import Health, RunRecord, WorkflowInput
from charge_agent.storage import Store

app = FastAPI(title="ChargeAIAgent", description="Durable sequential workflows")


@app.get("/health", response_model=Health)
def health() -> Health:
    with connect() as conn:
        conn.execute("SELECT 1 FROM engine.runs LIMIT 1")
    return Health()


@app.post("/runs", response_model=RunRecord, status_code=HTTPStatus.ACCEPTED)
def create_run(payload: WorkflowInput) -> RunRecord:
    with connect() as conn:
        store = Store(conn)
        run_id = store.create_run(payload)
        run = store.get_run(run_id)
        assert run is not None
        return run


@app.get("/runs/{run_id}", response_model=RunRecord)
def get_run(run_id: UUID) -> RunRecord:
    with connect() as conn:
        run = Store(conn).get_run(run_id)
    if run is not None:
        return run
    raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Run not found")
