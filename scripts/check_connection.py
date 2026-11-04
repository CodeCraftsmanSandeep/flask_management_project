"""Log in to Groww, fetch every dashboard section and print its *shape* - never its values.

Run with ``make smoke``. For each section it prints field names and their types (and record
counts), so the output is safe to paste into a chat or an issue: no balances, identifiers or
credentials ever appear. It doubles as a quick way to see what the Groww API really returns.
Read-only: nothing here can place or change an order.
"""

import sys
from typing import Any

from app import build_service
from app.config import Config
from app.errors import TradingError


def _type_name(value: Any) -> str:
    return "null" if value is None else type(value).__name__


def field_types(record: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Map each (nested) field name to the *type* of its value."""
    types: dict[str, str] = {}
    for key, value in record.items():
        if isinstance(value, dict):
            types.update(field_types(value, f"{prefix}{key}."))
        else:
            types[f"{prefix}{key}"] = _type_name(value)
    return types


def merged_field_types(records: list[dict[str, Any]]) -> dict[str, str]:
    """Like :func:`field_types` over many records; differing types are shown as ``int|float``."""
    seen: dict[str, set[str]] = {}
    for record in records:
        for name, kind in field_types(record).items():
            seen.setdefault(name, set()).add(kind)
    return {name: "|".join(sorted(kinds)) for name, kinds in sorted(seen.items())}


def _print_fields(types: dict[str, str]) -> None:
    for name, kind in types.items():
        print(f"             {name}: {kind}")


def main() -> int:
    service = build_service(Config.from_env())
    try:
        status = service.connect()
        print(f"login      OK  (token valid until {status['expires_at']})")

        print("profile    OK")
        _print_fields(field_types(service.get_profile()))

        print("funds      OK")
        _print_fields(field_types(service.get_funds()))

        holdings = service.get_holdings()
        print(f"holdings   OK  {len(holdings)} record(s)")
        _print_fields(merged_field_types(holdings))

        positions = service.get_positions()
        print(f"positions  OK  {len(positions)} record(s)")
        _print_fields(merged_field_types(positions))
    except TradingError as exc:
        print(f"FAILED [{exc.code}] {exc.message}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
