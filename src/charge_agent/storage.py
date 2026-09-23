from collections.abc import Callable
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from charge_agent.db import Connection
from charge_agent.models import (
    WORKFLOW,
    WORKFLOW_VERSION,
    RunRecord,
    State,
    StepRecord,
    ToolResult,
    WorkflowInput,
)

# One session lock guards the intentionally single-worker deployment. A second worker exits.
WORKER_LOCK_ID = 42190731


class Store:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def acquire_worker_lock(self) -> bool:
        row = self.conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK_ID,)
        ).fetchone()
        assert row is not None
        return bool(row["acquired"])

    def create_run(self, workflow_input: WorkflowInput) -> UUID:
        run_id = uuid4()
        payload = Jsonb(workflow_input.model_dump(mode="json"))
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO engine.runs (id, workflow_version, input) VALUES (%s, %s, %s)",
                (run_id, WORKFLOW_VERSION, payload),
            )
            for position, name in enumerate(WORKFLOW):
                key = f"{run_id}:{WORKFLOW_VERSION}:{position}:{name}"
                self.conn.execute(
                    """INSERT INTO engine.steps
                       (run_id, position, name, idempotency_key, input)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (run_id, position, name.value, key, payload),
                )
        return run_id

    def get_run(self, run_id: UUID) -> RunRecord | None:
        # A consistent snapshot prevents status/results from different worker commits.
        with self.conn.transaction():
            self.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            row = self.conn.execute("SELECT * FROM engine.runs WHERE id = %s", (run_id,)).fetchone()
            if row is None:
                return None
            steps = self.conn.execute(
                "SELECT * FROM engine.steps WHERE run_id = %s ORDER BY position", (run_id,)
            ).fetchall()
            return RunRecord.model_validate({**row, "steps": steps})

    def next_step(self) -> StepRecord | None:
        # Only the earliest unfinished step is eligible. Future retry waits survive restart.
        row = self.conn.execute(
            """SELECT s.* FROM engine.steps s JOIN engine.runs r ON r.id = s.run_id
               WHERE r.state IN ('pending', 'running')
                 AND r.workflow_version = %s
                 AND s.state IN ('pending', 'running', 'retry_wait')
                 AND (s.next_attempt_at IS NULL OR s.next_attempt_at <= clock_timestamp())
                 AND NOT EXISTS (
                     SELECT 1 FROM engine.steps earlier WHERE earlier.run_id = s.run_id
                       AND earlier.position < s.position AND earlier.state <> 'completed')
               ORDER BY r.created_at, s.position LIMIT 1""",
            (WORKFLOW_VERSION,),
        ).fetchone()
        return StepRecord.model_validate(row) if row else None

    def begin(self, step: StepRecord) -> StepRecord:
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE engine.runs SET state = 'running' WHERE id = %s", (step.run_id,)
            )
            row = self.conn.execute(
                """UPDATE engine.steps SET state = 'running', attempts = attempts + 1,
                   next_attempt_at = NULL, outcome_unknown = true
                   WHERE run_id = %s AND position = %s RETURNING *""",
                (step.run_id, step.position),
            ).fetchone()
        return StepRecord.model_validate(row)

    def complete(
        self, step: StepRecord, result: ToolResult, during_commit: Callable[[], None]
    ) -> None:
        with self.conn.transaction():
            self.conn.execute(
                """UPDATE engine.steps SET state = 'completed', result = %s,
                   outcome_unknown = false, error = NULL, next_attempt_at = NULL
                   WHERE run_id = %s AND position = %s""",
                (Jsonb(result.model_dump(mode="json")), step.run_id, step.position),
            )
            during_commit()
            self.conn.execute(
                """UPDATE engine.runs SET state = 'completed' WHERE id = %s
                   AND NOT EXISTS (SELECT 1 FROM engine.steps
                       WHERE run_id = %s AND state <> 'completed')""",
                (step.run_id, step.run_id),
            )

    def record_failure(
        self, step: StepRecord, message: str, delay: float | None, unknown: bool
    ) -> None:
        terminal = State.NEEDS_REVIEW if unknown else State.FAILED
        state = State.RETRY_WAIT if delay is not None else terminal
        with self.conn.transaction():
            self.conn.execute(
                """UPDATE engine.steps SET state = %s, failures = failures + 1, error = %s,
                   outcome_unknown = %s,
                   next_attempt_at = CASE WHEN %s::double precision IS NULL THEN NULL
                     ELSE clock_timestamp() + %s * interval '1 second' END
                   WHERE run_id = %s AND position = %s""",
                (state.value, message, unknown, delay, delay, step.run_id, step.position),
            )
            if delay is not None:
                return
            self.conn.execute(
                "UPDATE engine.runs SET state = %s WHERE id = %s",
                (terminal.value, step.run_id),
            )
