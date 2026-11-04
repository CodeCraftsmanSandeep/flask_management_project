"""Apply the SQL files in ``app/db/migrations`` once each, in name order.

    python -m app.db.migrate            # uses DATABASE_URL or the project default

Each file runs in its own transaction together with its bookkeeping row, so a failing migration
leaves nothing half-applied. An advisory lock stops two processes migrating at the same time.
Migrations are append-only: never edit one that has been applied, add a new file instead.
"""

import logging
import os
import sys
from pathlib import Path

import psycopg

from app.config import DEFAULT_DATABASE_URL

from .connection import connect

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
LOCK_KEY = 7_424_001  # arbitrary, only needs to be unique to this app
logger = logging.getLogger(__name__)


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    return sorted(directory.glob("*.sql"))


def migrate(url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations; return the names applied this time."""
    applied: list[str] = []
    with connect(url) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            conn.commit()
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            conn.commit()
            for path in migration_files(directory):
                if path.name in done:
                    continue
                try:
                    conn.execute(path.read_text())
                    conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,)
                    )
                    conn.commit()
                except psycopg.Error:
                    conn.rollback()
                    logger.exception("migration %s failed and was rolled back", path.name)
                    raise
                applied.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
            conn.commit()
    return applied


def main() -> int:
    url = os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL
    try:
        applied = migrate(url)
    except psycopg.OperationalError as exc:
        print(f"Cannot reach Postgres ({exc}). Is it running? Try: make db-start", file=sys.stderr)
        return 1
    print(f"Applied {len(applied)} migration(s): {', '.join(applied) or 'nothing to do'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
