"""Load the Groww TOTP credentials from ``keys.txt`` (or environment variables).

``keys.txt`` holds one ``NAME=value`` per line (``NAME: value`` works too). Blank lines and ``#``
comments are ignored and values may be quoted. Only the two TOTP entries are ever read - every
other line (for example the API-key/secret pair used by Groww's *other* login flow) is skipped
and never kept in memory.

* TOTP token  - the long JWT labelled "TOTP token" on the Groww Cloud API keys page
* TOTP secret - the 32-character base32 string behind the QR code on the same page

Environment variables with the same names take precedence over the file.
"""

import base64
import binascii
import logging
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CredentialsError

logger = logging.getLogger(__name__)

# The first name in each tuple is the canonical one. The others are spellings that exist in real
# keys files (e.g. "TOP" for "TOTP"), so a file does not need editing to be understood.
TOKEN_NAMES = ("GROWW_TOTP_TOKEN", "GROWW_TOP_TOKEN")
SECRET_NAMES = ("GROWW_TOTP_SECRET", "GROWW_TOTP_QR_CODE", "GROWW_TOP_QR_CODE")

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][\w .-]*?)\s*[=:]\s*(.*?)\s*$")


@dataclass(frozen=True, slots=True)
class TotpCredentials:
    """The pair needed for Groww's TOTP login. ``repr`` deliberately shows no values."""

    token: str = field(repr=False)
    secret: str = field(repr=False)


def parse_keys(text: str, wanted: frozenset[str]) -> dict[str, str]:
    """Return ``{NAME: value}`` for the *wanted* names only; everything else is discarded."""
    found: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if match is None:
            continue
        name = re.sub(r"[ .-]+", "_", match.group(1)).upper()
        if name in wanted:
            found[name] = _unquote(match.group(2))
    return found


def _unquote(value: str) -> str:
    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def _first(values: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = values.get(name, "").strip()
        if value:
            return value
    return None


def _read_keys_file(path: Path, wanted: frozenset[str]) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
        mode = path.stat().st_mode
    except FileNotFoundError:
        msg = (
            f"Credentials file not found: {path}. Create it with lines like "
            f"{TOKEN_NAMES[0]}=<token> and {SECRET_NAMES[0]}=<secret> "
            "(or set those environment variables)."
        )
        raise CredentialsError(msg) from None
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"Could not read {path} ({type(exc).__name__})."
        raise CredentialsError(msg) from None

    if mode & (stat.S_IRGRP | stat.S_IROTH):
        logger.warning(
            "%s can be read by other users on this machine; run: chmod 600 %s", path, path
        )
    return parse_keys(text, wanted)


def _validated_secret(raw: str) -> str:
    secret = re.sub(r"[\s-]", "", raw).upper()  # authenticator apps show it in groups
    unpadded = secret.rstrip("=")
    try:
        base64.b32decode(unpadded + "=" * (-len(unpadded) % 8))
    except (binascii.Error, ValueError):
        msg = (
            "The TOTP secret is not valid base32. It should be about 32 characters made of "
            "letters A-Z and digits 2-7."
        )
        raise CredentialsError(msg) from None
    return secret


def load_credentials(path: Path, env: Mapping[str, str] | None = None) -> TotpCredentials:
    """Read and validate the TOTP token + secret. Error messages never contain their values."""
    env = os.environ if env is None else env
    wanted = frozenset(TOKEN_NAMES + SECRET_NAMES)

    from_env = {name: env[name] for name in wanted if name in env}
    token = _first(from_env, TOKEN_NAMES)
    secret = _first(from_env, SECRET_NAMES)

    if token is None or secret is None:
        from_file = _read_keys_file(path, wanted)
        token = token or _first(from_file, TOKEN_NAMES)
        secret = secret or _first(from_file, SECRET_NAMES)

    if token is None:
        msg = f"No TOTP token found in {path.name}. Add a line like {TOKEN_NAMES[0]}=<token>."
        raise CredentialsError(msg)
    if secret is None:
        msg = f"No TOTP secret found in {path.name}. Add a line like {SECRET_NAMES[0]}=<secret>."
        raise CredentialsError(msg)
    if re.search(r"\s", token):
        msg = "The TOTP token contains whitespace; it must be a single unbroken string."
        raise CredentialsError(msg)

    return TotpCredentials(token=token, secret=_validated_secret(secret))
