import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlparse

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from charge_agent.config import settings
from charge_agent.db import Connection

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[Connection]:
    url = os.environ.get(
        "CHARGE_TEST_DATABASE_URL", "postgresql://charge:charge@localhost:5432/charge_test"
    )
    if not urlparse(url).path.endswith("_test"):
        raise ValueError("Integration tests require a dedicated database ending in _test")
    monkeypatch.setenv("CHARGE_DATABASE_URL", url)
    monkeypatch.setattr(settings, "database_url", url)
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute((ROOT / "schema.sql").read_text())
        conn.execute("TRUNCATE engine.steps, engine.runs, mock_tool.effects, mock_tool.requests")
        yield conn


def wait_until(check: Callable[[], bool], timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.03)
    raise AssertionError("Timed out waiting for condition")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Processes:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.children: list[subprocess.Popen[bytes]] = []

    def start(
        self, args: list[str], overrides: dict[str, str] | None = None
    ) -> tuple[subprocess.Popen[bytes], Path]:
        env = {
            **os.environ,
            "CHARGE_TOOL_FAILURE_RATE": "0",
            "CHARGE_TOOL_FAIL_FIRST": "0",
            "CHARGE_TOOL_RESPONSE_DELAY_SECONDS": "0",
            "CHARGE_PAUSE_AT": "",
            "CHARGE_POLL_SECONDS": "0.03",
            **(overrides or {}),
        }
        log = self.directory / f"process-{len(self.children)}.log"
        with log.open("wb") as output:
            process = subprocess.Popen(
                [sys.executable, *args], env=env, stdout=output, stderr=subprocess.STDOUT, cwd=ROOT
            )
        self.children.append(process)
        return process, log

    def service(self, module: str, overrides: dict[str, str] | None = None) -> str:
        port = free_port()
        process, log = self.start(
            ["-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)], overrides
        )
        url = f"http://127.0.0.1:{port}"

        def ready() -> bool:
            assert process.poll() is None, log.read_text()
            try:
                return httpx.get(f"{url}/health", timeout=0.5).is_success
            except httpx.TransportError:
                return False

        wait_until(ready)
        return url

    def worker(self, url: str, pause: str = "", **env: str) -> tuple[subprocess.Popen[bytes], Path]:
        return self.start(
            ["-m", "charge_agent.worker"], {"CHARGE_TOOL_URL": url, "CHARGE_PAUSE_AT": pause, **env}
        )

    def close(self) -> None:
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)


@pytest.fixture
def processes(database: Connection, tmp_path: Path) -> Iterator[Processes]:
    pool = Processes(tmp_path)
    try:
        yield pool
    finally:
        pool.close()
