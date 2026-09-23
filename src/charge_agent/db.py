from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from charge_agent.config import settings

Connection = psycopg.Connection[dict[str, Any]]


@contextmanager
def connect() -> Iterator[Connection]:
    # Autocommit keeps reads out of long-lived transactions; writes use explicit transactions.
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        yield conn
