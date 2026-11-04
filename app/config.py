"""Runtime configuration, read from environment variables.

======================  ===========================================  ==================
Variable                Meaning                                      Default
======================  ===========================================  ==================
GROWW_KEYS_FILE         Path to the file holding the TOTP credentials ``<project>/keys.txt``
GROWW_REQUEST_TIMEOUT   Seconds to wait for each Groww data call      ``10``
ALLOWED_HOSTS           Extra Host-header names to accept (comma      none
                        separated). localhost/127.0.0.1/::1 always
                        work.
DATABASE_URL            Postgres holding candles and cached trades    see ``DEFAULT_DATABASE_URL``
ACCOUNTS_FILE           Where added broker accounts and their keys    ``secrets/accounts.json``
                        are kept
CONTACT_URL             Where the "Message me" buttons lead (https)   ``DEFAULT_CONTACT_URL``
======================  ===========================================  ==================

Host and port are Flask CLI options (``make run`` / ``make dev``), not part of this config.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 5001
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_DATABASE_URL = "postgresql://trading@127.0.0.1:5433/trading"  # `make db-start`
DEFAULT_CONTACT_URL = "https://www.upwork.com/freelancers/~01d59b53fbdfc2b68b?viewMode=1"


@dataclass(frozen=True, slots=True)
class Config:
    keys_file: Path = PROJECT_ROOT / "keys.txt"
    request_timeout: int = DEFAULT_TIMEOUT_SECONDS
    allowed_hosts: frozenset[str] = LOCAL_HOSTS
    database_url: str = DEFAULT_DATABASE_URL
    accounts_file: Path = PROJECT_ROOT / "secrets" / "accounts.json"
    contact_url: str = DEFAULT_CONTACT_URL

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        env = os.environ if env is None else env

        keys_file = PROJECT_ROOT / "keys.txt"
        if env.get("GROWW_KEYS_FILE"):
            keys_file = Path(env["GROWW_KEYS_FILE"]).expanduser().resolve()

        raw_timeout = env.get("GROWW_REQUEST_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS))
        try:
            timeout = int(raw_timeout)
        except ValueError:
            msg = "GROWW_REQUEST_TIMEOUT must be a whole number of seconds"
            raise ValueError(msg) from None
        if timeout <= 0:
            msg = "GROWW_REQUEST_TIMEOUT must be greater than zero"
            raise ValueError(msg)

        extra_hosts = {h.strip().lower() for h in env.get("ALLOWED_HOSTS", "").split(",")}
        extra_hosts.discard("")

        accounts_file = PROJECT_ROOT / "secrets" / "accounts.json"
        if env.get("ACCOUNTS_FILE"):
            accounts_file = Path(env["ACCOUNTS_FILE"]).expanduser().resolve()

        contact_url = (env.get("CONTACT_URL") or DEFAULT_CONTACT_URL).strip()
        if not contact_url.lower().startswith("https://"):
            msg = "CONTACT_URL must be an https:// link"
            raise ValueError(msg)

        return cls(
            keys_file=keys_file,
            contact_url=contact_url,
            accounts_file=accounts_file,
            request_timeout=timeout,
            allowed_hosts=LOCAL_HOSTS | extra_hosts,
            database_url=env.get("DATABASE_URL") or DEFAULT_DATABASE_URL,
        )
