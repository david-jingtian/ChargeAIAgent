import json
import signal
import threading
from types import FrameType

import httpx

from charge_agent.config import settings
from charge_agent.db import connect
from charge_agent.engine import Engine
from charge_agent.models import StepRecord
from charge_agent.storage import Store
from charge_agent.tool_client import ToolClient

PAUSE_POINTS = {"before_tool", "after_tool", "during_commit", "after_commit"}


def main() -> None:
    if settings.pause_at and settings.pause_at not in PAUSE_POINTS:
        raise ValueError(f"Unknown pause point: {settings.pause_at}")
    stop = threading.Event()

    def on_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    def hook(point: str, step: StepRecord) -> None:
        if point != settings.pause_at or step.name != settings.pause_step or step.attempts != 1:
            return
        print(
            json.dumps({"event": "paused", "point": point, "key": step.idempotency_key}), flush=True
        )
        stop.wait()  # Demo/test barrier. SIGKILL bypasses all cleanup.
        raise SystemExit(0)  # Roll back any open transaction on a graceful exit.

    with connect() as conn:
        store = Store(conn)
        if not store.acquire_worker_lock():
            raise SystemExit("Another worker owns this database; single-worker mode only")
        with httpx.Client(
            base_url=settings.tool_url,
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
        ) as client:
            engine = Engine(store, ToolClient(client), settings, hook)
            print(json.dumps({"event": "worker_ready"}), flush=True)
            while not stop.is_set():
                if not engine.tick():
                    stop.wait(settings.poll_seconds)
        # Database exceptions are deliberately fatal: never reconnect and continue without lock.


if __name__ == "__main__":
    main()
