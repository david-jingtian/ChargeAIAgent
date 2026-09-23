import hashlib
from uuid import uuid4

from psycopg.types.json import Jsonb

from charge_agent.db import Connection
from charge_agent.models import EffectRecord, StepName, ToolResult, WorkflowInput


class KeyConflict(Exception):
    pass


class ToolStore:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def request_count(self, key: str) -> int:
        row = self.conn.execute(
            """INSERT INTO mock_tool.requests VALUES (%s, 1)
               ON CONFLICT (idempotency_key) DO UPDATE
               SET count = mock_tool.requests.count + 1 RETURNING count""",
            (key,),
        ).fetchone()
        assert row is not None
        return int(row["count"])

    def existing(self, key: str, operation: StepName, payload: WorkflowInput) -> ToolResult | None:
        row = self.conn.execute(
            "SELECT * FROM mock_tool.effects WHERE idempotency_key = %s", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation.value or row["input"] != payload.model_dump(mode="json"):
            raise KeyConflict("Idempotency key reused with a different operation or input")
        return ToolResult.model_validate(row["result"])

    def execute(self, key: str, operation: StepName, payload: WorkflowInput) -> ToolResult:
        result = ToolResult(effect_id=uuid4(), operation=operation, input=payload)
        with self.conn.transaction():
            # Unique constraint handles overlapping requests, including a timed-out predecessor.
            self.conn.execute(
                """INSERT INTO mock_tool.effects (idempotency_key, operation, input, result)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING""",
                (
                    key,
                    operation.value,
                    Jsonb(payload.model_dump(mode="json")),
                    Jsonb(result.model_dump(mode="json")),
                ),
            )
            saved = self.existing(key, operation, payload)
            assert saved is not None
        return saved

    def effects(self, prefix: str) -> list[EffectRecord]:
        # starts_with treats user input literally, unlike a LIKE pattern.
        rows = self.conn.execute(
            """SELECT idempotency_key, result, created_at FROM mock_tool.effects
               WHERE starts_with(idempotency_key, %s) ORDER BY created_at""",
            (prefix,),
        ).fetchall()
        return [EffectRecord.model_validate(row) for row in rows]


def failure_roll(seed: int, key: str, attempt: int) -> float:
    """Stable pseudorandom failures: restart does not reset a shared PRNG sequence."""
    digest = hashlib.sha256(f"{seed}:{key}:{attempt}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64
