"""A repeatable live SIGKILL demo. Run with Python 3.12 and Docker Compose installed."""

import json
import os
import subprocess
import time
import urllib.request
from collections.abc import Callable
from typing import Any

ENV = {
    **os.environ,
    "CHARGE_PAUSE_AT": "after_tool",
    "CHARGE_TOOL_FAILURE_RATE": "0",
    "CHARGE_TOOL_FAIL_FIRST": "0",
    "CHARGE_TOOL_RESPONSE_DELAY_SECONDS": "0",
}
API = "http://127.0.0.1:8000"
TOOL = "http://127.0.0.1:8001"


def compose(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        ["docker", "compose", *args],
        env=ENV,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout or ""


def request(url: str, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def wait_for(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.2)
    raise RuntimeError("Demo timed out; inspect docker compose logs")


def main() -> None:
    compose("up", "--build", "-d", "--wait")
    run = request(f"{API}/runs", {"customer_id": "crash-demo", "amount_cents": 2500})
    run_id = run["id"]
    key = run["steps"][0]["idempotency_key"]

    def paused() -> bool:
        logs = compose("logs", "--no-color", "worker", capture=True)
        return any('"event": "paused"' in line and key in line for line in logs.splitlines())

    wait_for(paused)
    before = request(f"{API}/runs/{run_id}")
    effects = request(f"{TOOL}/effects?prefix={run_id}")
    assert before["steps"][0]["state"] == "running"
    assert len(effects) == 1
    effect_id = effects[0]["result"]["effect_id"]
    print(f"\nCharge exists: {effect_id}; engine step still running.", flush=True)
    print("Killing the worker with SIGKILL (equivalent to kill -9)…", flush=True)
    compose("kill", "-s", "SIGKILL", "worker")
    compose("start", "worker")
    wait_for(lambda: request(f"{API}/runs/{run_id}")["state"] == "completed")
    after = request(f"{API}/runs/{run_id}")
    effects = request(f"{TOOL}/effects?prefix={run_id}")
    charges = [item for item in effects if item["result"]["operation"] == "charge"]
    assert len(charges) == 1 and charges[0]["result"]["effect_id"] == effect_id
    assert after["steps"][0]["attempts"] == 2
    assert len(effects) == 3
    print(json.dumps(after, indent=2))
    print("PASS: workflow completed; charge attempted twice, side effect recorded exactly once.")
    print("Restore normal worker settings: docker compose up -d --force-recreate tool worker")


if __name__ == "__main__":
    main()
