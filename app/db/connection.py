"""Connections to the local Postgres. One short-lived connection per unit of work."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

CONNECT_TIMEOUT_SECONDS = 5


def connect(url: str) -> psycopg.Connection:
    """Open a connection (transactions are explicit: callers commit)."""
    return psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_SECONDS)


@contextmanager
def session(url: str) -> Iterator[psycopg.Connection]:
    """A connection that commits on success, rolls back on error and always closes."""
    conn = connect(url)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
