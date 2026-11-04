"""Import NIFTY spot and option 1-minute candles from CSV files into the same tables Groww fills.

Any data provider will do, as long as it can export 1-minute candles. The importer never guesses:
every file is **validated completely before anything is written**, and what it cannot be sure of
(which end of the minute a timestamp marks, where the expiry dates come from, whether a file
holds another index) is either decided from the data, taken from an explicit option, or reported
as a warning.

File formats (header row required, column names are case-insensitive, extra columns are ignored):

``--spot``  one row per minute of the NIFTY 50 index
    timestamp | datetime            ``2026-05-12 09:15`` / ``2026-05-12T09:15:00+05:30`` /
                                    ``12-05-2026 09:15`` (day first) / epoch seconds or milliseconds
    or  date + time                 two columns that are joined
    open, high, low, close          (also ``o h l c``), at most 4 decimals
    volume                          optional (index volume is often missing: 0 is stored)
    If the file has a ``symbol`` / ``underlying`` / ``name`` column, only NIFTY 50 rows are read.

``--options``  one row per contract per minute
    the same time and price columns, plus optional ``volume`` and ``oi`` / ``open_interest``, plus
    the contract, given either as
      * ``expiry`` + ``strike`` + ``option_type`` (CE/PE, also CALL/PUT, C/P)   or
      * ``symbol`` written as ``NSE-NIFTY-12May26-24000-CE``.
    A ``underlying`` / ``name`` column, or a symbol of another index, makes the row be skipped
    (and counted); without either, every row is taken to be NIFTY.

``--expiries``  one expiry date per line (a header is fine). ``--derive-expiries`` instead uses the
    expiries found in the option files, which is only right if the files hold *every* weekly
    expiry in the range (otherwise the app could trade a later expiry than the real nearest one).

Times are IST wall-clock. A time with an offset (``+00:00``, ``Z``, ``+05:30``) is converted to IST;
a time without one is taken to be IST already. Whether a stamp marks the *start* or the *end* of its
minute is detected from the index's first and last candle of each day (09:15 / 15:29 vs 09:16 /
15:30) and applied to the options too.
"""

import csv
import gzip
import re
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO, Literal

from app.errors import TradingError
from app.history.backfill import CONVENTION_KEY, SPOT_SYMBOL, UNDERLYING
from app.history.quality import (
    Convention,
    DayQuality,
    assess_day,
    detect_convention,
    regular_session_only,
    to_start_convention,
)
from app.history.store import FetchKind, FetchStatus, Store
from app.history.synthetic import DATA_SOURCE_KEY, SYNTHETIC_SOURCE
from app.strategy.bars import in_session
from app.strategy.models import Candle, OptionType
from app.strategy.options import expiry_from_symbol, groww_symbol, pick_expiry
from app.timeutil import IST, to_naive_ist

CSV_SOURCE = "csv_import"
TimestampMode = Literal["auto", "start", "close"]

_ONE_MINUTE = timedelta(minutes=1)
_MAX_DECIMALS = 4  # the database stores prices with 4 decimals; more would be rounded silently
_MILLISECOND_THRESHOLD = 100_000_000_000
_PLAUSIBLE_YEARS = range(2000, 2101)
_MAX_LISTED = 5  # how many examples a note shows

_STAMP = ("timestamp", "datetime", "date_time", "time_stamp", "ts")
_DATE = ("date", "trade_date", "day")
_CLOCK = ("time", "minute")
_OPEN = ("open", "o")
_HIGH = ("high", "h")
_LOW = ("low", "l")
_CLOSE = ("close", "c")
_VOLUME = ("volume", "vol", "v")
_OI = ("oi", "open_interest", "openinterest")
_SYMBOL = ("symbol", "tradingsymbol", "trading_symbol", "contract", "instrument")
_EXPIRY = ("expiry", "expiry_date", "expiration", "expiration_date")
_STRIKE = ("strike", "strike_price")
_KIND = ("option_type", "type", "right", "cp", "instrument_type", "opt_type")
_UNDERLYING = ("underlying", "name", "index", "root")

_KINDS = {
    "CE": OptionType.CE,
    "C": OptionType.CE,
    "CALL": OptionType.CE,
    "PE": OptionType.PE,
    "P": OptionType.PE,
    "PUT": OptionType.PE,
}
_DAY_FIRST_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%b-%y", "%d%b%Y", "%d%b%y")
_STAMP_FORMATS = (
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y %H:%M",
)
_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ T].*)?$")
_ISO_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


class ImportDataError(TradingError):
    """A file cannot be imported as it is (the message says which line and why)."""

    code = "import_error"
    http_status = 400


class _RowError(ValueError):
    """One row is unusable; the reader adds the file name and line number."""


# --------------------------------------------------------------------------- value parsing


def parse_stamp(text: str) -> datetime:
    """A candle time as naive IST, to the whole minute. Raises ``ValueError`` if it is unusable."""
    value = text.strip()
    if not value:
        msg = "the time is empty"
        raise ValueError(msg)
    parsed: datetime | None = None
    if _NUMBER.match(value):
        number = float(value)
        seconds = number / 1000 if abs(number) >= _MILLISECOND_THRESHOLD else number
        try:
            parsed = to_naive_ist(datetime.fromtimestamp(seconds, IST))
        except (OverflowError, OSError, ValueError) as exc:
            msg = f"the time {text!r} is out of range"
            raise ValueError(msg) from exc
    elif _ISO_STAMP.match(value):
        try:
            parsed = to_naive_ist(datetime.fromisoformat(value))
        except ValueError as exc:
            msg = f"the time {text!r} is not a valid date and time"
            raise ValueError(msg) from exc
    else:
        for pattern in _STAMP_FORMATS:
            try:
                parsed = datetime.strptime(value.title(), pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        msg = (
            f"the time {text!r} is not understood (use e.g. 2026-05-12 09:15, "
            "12-05-2026 09:15 with the day first, or epoch seconds)"
        )
        raise ValueError(msg)
    if parsed.year not in _PLAUSIBLE_YEARS:
        msg = f"the time {text!r} is outside the years 2000-2100"
        raise ValueError(msg)
    if parsed.second or parsed.microsecond:
        msg = f"the time {text!r} is not on a whole minute"
        raise ValueError(msg)
    return parsed


def parse_date(text: str) -> date:
    """A calendar date: ISO (2026-05-12) or day-first (12-05-2026, 12/05/2026, 12-May-2026)."""
    value = text.strip()
    iso = _ISO_DATE.match(value)
    if iso:
        try:
            return date.fromisoformat(iso.group(1))
        except ValueError as exc:
            msg = f"{text!r} is not a valid date"
            raise ValueError(msg) from exc
    for pattern in _DAY_FIRST_FORMATS:
        try:
            return datetime.strptime(value.title(), pattern).date()
        except ValueError:
            continue
    msg = f"the date {text!r} is not understood (use 2026-05-12, or 12-05-2026 with the day first)"
    raise ValueError(msg)


def _price(text: str, name: str) -> Decimal:
    if not text:
        msg = f"{name} is empty"
        raise _RowError(msg)
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        msg = f"{name} {text!r} is not a number"
        raise _RowError(msg) from exc
    if not number.is_finite() or number < 0:
        msg = f"{name} {text!r} is not a finite, non-negative number"
        raise _RowError(msg)
    if number.normalize().as_tuple().exponent < -_MAX_DECIMALS:  # type: ignore[operator]
        msg = f"{name} {text!r} has more than {_MAX_DECIMALS} decimal places"
        raise _RowError(msg)
    return number


def _count(text: str, name: str) -> int | None:
    """A whole non-negative number such as volume; blank means "not given"."""
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        msg = f"{name} {text!r} is not a number"
        raise _RowError(msg) from exc
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        msg = f"{name} {text!r} is not a whole non-negative number"
        raise _RowError(msg)
    return int(number)


def _strike(text: str) -> int:
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        msg = f"strike {text!r} is not a number"
        raise _RowError(msg) from exc
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        msg = f"strike {text!r} is not a positive whole number"
        raise _RowError(msg)
    return int(number)


def _kind(text: str) -> OptionType:
    kind = _KINDS.get(text.strip().upper())
    if kind is None:
        msg = f"option type {text!r} is not CE/PE (or CALL/PUT, C/P)"
        raise _RowError(msg)
    return kind


def _is_nifty(text: str) -> bool:
    """``NIFTY``, ``NIFTY 50``, ``NSE:NIFTY50``, ``NSE-NIFTY`` ... but not BANKNIFTY and friends."""
    cleaned = re.sub(r"[\s_:\-]+", "", text.upper())
    cleaned = cleaned.removeprefix("NSE")
    return cleaned in ("NIFTY", "NIFTY50")


def _parse_symbol(text: str) -> tuple[str, date] | None:
    """``NSE-NIFTY-12May26-24000-CE`` -> (canonical symbol, expiry); None for another index."""
    parts = text.strip().split("-")
    if len(parts) != 5 or parts[0].upper() != "NSE":
        msg = (
            f"symbol {text!r} is not in the form NSE-NIFTY-12May26-24000-CE; "
            "give expiry, strike and option_type columns instead"
        )
        raise _RowError(msg)
    if not _is_nifty(parts[1]):
        return None
    try:
        expiry = datetime.strptime(parts[2].title(), "%d%b%y").date()
    except ValueError as exc:
        msg = f"symbol {text!r} has an expiry that is not like 12May26"
        raise _RowError(msg) from exc
    return groww_symbol(expiry, _strike(parts[3]), _kind(parts[4])), expiry


# ---------------------------------------------------------------------------- file reading


@dataclass(slots=True)
class ReadStats:
    """What the readers saw, beyond the candles themselves."""

    rows: int = 0
    bad_rows: int = 0
    other_instruments: int = 0
    examples: list[str] = field(default_factory=list)

    def bad(self, text: str) -> None:
        self.bad_rows += 1
        if len(self.examples) < _MAX_LISTED:
            self.examples.append(text)


@contextmanager
def _open(path: Path) -> Iterator[IO[str]]:
    try:
        if path.suffix.lower() == ".gz":
            with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
                yield handle
        else:
            with path.open("rt", encoding="utf-8-sig", newline="") as handle:
                yield handle
    except FileNotFoundError as exc:
        msg = f"File not found: {path}"
        raise ImportDataError(msg) from exc
    except (OSError, UnicodeDecodeError, EOFError, gzip.BadGzipFile) as exc:
        msg = f"{path.name} cannot be read as a text/CSV file ({type(exc).__name__})"
        raise ImportDataError(msg) from exc


def _table(path: Path, delimiter: str) -> Iterator[tuple[list[str], list[str], int]]:
    """Yield (header names, row, line number) for every non-blank row of a CSV file."""
    with _open(path) as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        header = next(reader, None)
        if not header or not any(cell.strip() for cell in header):
            msg = f"{path.name} is empty (a header row is required)"
            raise ImportDataError(msg)
        names = [cell.strip().lower().replace(" ", "_") for cell in header]
        for row in reader:
            if not row or not any(cell.strip() for cell in row):
                continue
            yield names, row, reader.line_num


def _find(names: Sequence[str], aliases: Sequence[str]) -> int | None:
    for alias in aliases:
        if alias in names:
            return names.index(alias)
    return None


def _cell(row: Sequence[str], index: int) -> str:
    try:
        return row[index].strip()
    except IndexError as exc:
        msg = "the row has too few fields"
        raise _RowError(msg) from exc


@dataclass(frozen=True, slots=True)
class _CandleColumns:
    stamp: int
    clock: int | None
    open: int
    high: int
    low: int
    close: int
    volume: int | None
    oi: int | None


def _candle_columns(names: Sequence[str], path: Path) -> _CandleColumns:
    stamp = _find(names, _STAMP)
    clock = None
    if stamp is None:
        stamp, clock = _find(names, _DATE), _find(names, _CLOCK)
        if stamp is None or clock is None:
            msg = (
                f"{path.name}: no time column found. Use a 'timestamp' (or 'datetime') column, "
                f"or both 'date' and 'time'. Columns present: {', '.join(names)}"
            )
            raise ImportDataError(msg)
    prices = [_find(names, aliases) for aliases in (_OPEN, _HIGH, _LOW, _CLOSE)]
    if any(p is None for p in prices):
        missing = [
            label
            for label, index in zip(("open", "high", "low", "close"), prices, strict=True)
            if index is None
        ]
        msg = (
            f"{path.name}: missing column(s) {', '.join(missing)}. "
            f"Columns present: {', '.join(names)}"
        )
        raise ImportDataError(msg)
    o, h, low, c = (int(p) for p in prices if p is not None)
    return _CandleColumns(stamp, clock, o, h, low, c, _find(names, _VOLUME), _find(names, _OI))


def _read_candle(row: Sequence[str], cols: _CandleColumns) -> Candle:
    stamp = _cell(row, cols.stamp)
    if cols.clock is not None:
        stamp = f"{stamp} {_cell(row, cols.clock)}"
    try:
        start = parse_stamp(stamp)
    except ValueError as exc:
        raise _RowError(str(exc)) from exc
    volume = _count(_cell(row, cols.volume), "volume") if cols.volume is not None else None
    oi = _count(_cell(row, cols.oi), "open interest") if cols.oi is not None else None
    return Candle(
        start=start,
        open=_price(_cell(row, cols.open), "open"),
        high=_price(_cell(row, cols.high), "high"),
        low=_price(_cell(row, cols.low), "low"),
        close=_price(_cell(row, cols.close), "close"),
        volume=volume or 0,
        oi=oi,
    )


def _failed(path: Path, line: int, exc: _RowError, lenient: bool, stats: ReadStats) -> None:
    text = f"{path.name} line {line}: {exc}"
    if not lenient:
        raise ImportDataError(text) from exc
    stats.bad(text)


def iter_spot(
    path: Path, *, delimiter: str = ",", lenient: bool = False, stats: ReadStats | None = None
) -> Iterator[Candle]:
    """The NIFTY 50 candles in one spot file (in file order, duplicates included)."""
    stats = stats if stats is not None else ReadStats()
    cols: _CandleColumns | None = None
    who: int | None = None
    for names, row, line in _table(path, delimiter):
        if cols is None:
            cols = _candle_columns(names, path)
            who = _find(names, _UNDERLYING)
            if who is None:
                who = _find(names, _SYMBOL)
        stats.rows += 1
        try:
            if who is not None and not _is_nifty(_cell(row, who)):
                stats.other_instruments += 1
                continue
            candle = _read_candle(row, cols)
        except _RowError as exc:
            _failed(path, line, exc, lenient, stats)
            continue
        yield candle


def iter_options(
    path: Path, *, delimiter: str = ",", lenient: bool = False, stats: ReadStats | None = None
) -> Iterator[tuple[str, Candle]]:
    """``(canonical symbol, candle)`` for every NIFTY option row of one file."""
    stats = stats if stats is not None else ReadStats()
    cols: _CandleColumns | None = None
    expiry_at: int | None = None
    strike_at: int | None = None
    kind_at: int | None = None
    symbol_at: int | None = None
    who: int | None = None
    for names, row, line in _table(path, delimiter):
        if cols is None:
            cols = _candle_columns(names, path)
            expiry_at, strike_at = _find(names, _EXPIRY), _find(names, _STRIKE)
            kind_at, symbol_at = _find(names, _KIND), _find(names, _SYMBOL)
            who = _find(names, _UNDERLYING)
            if None in (expiry_at, strike_at, kind_at) and symbol_at is None:
                msg = (
                    f"{path.name}: cannot tell which contract each row is. Provide 'expiry', "
                    "'strike' and 'option_type' columns, or a 'symbol' column like "
                    f"NSE-NIFTY-12May26-24000-CE. Columns present: {', '.join(names)}"
                )
                raise ImportDataError(msg)
        stats.rows += 1
        try:
            if who is not None and not _is_nifty(_cell(row, who)):
                stats.other_instruments += 1
                continue
            if expiry_at is not None and strike_at is not None and kind_at is not None:
                try:
                    expiry = parse_date(_cell(row, expiry_at))
                except ValueError as exc:
                    raise _RowError(str(exc)) from exc
                symbol = groww_symbol(
                    expiry, _strike(_cell(row, strike_at)), _kind(_cell(row, kind_at))
                )
            else:
                assert symbol_at is not None  # noqa: S101 - checked with the header above
                parsed = _parse_symbol(_cell(row, symbol_at))
                if parsed is None:
                    stats.other_instruments += 1
                    continue
                symbol = parsed[0]
            candle = _read_candle(row, cols)
        except _RowError as exc:
            _failed(path, line, exc, lenient, stats)
            continue
        yield symbol, candle


def read_expiries(path: Path, *, delimiter: str = ",") -> list[date]:
    """Expiry dates from a one-column list (a header such as ``expiry`` is allowed)."""
    found: set[date] = set()
    with _open(path) as handle:
        rows = list(csv.reader(handle, delimiter=delimiter))
    column = 0
    for index, row in enumerate(rows):
        if not row or not any(cell.strip() for cell in row):
            continue
        if index == 0:
            names = [cell.strip().lower() for cell in row]
            header = _find(names, _EXPIRY)
            if header is not None:
                column = header
                continue
            if all(not any(ch.isdigit() for ch in cell) for cell in names):
                continue  # some other header word: the first column holds the dates
        try:
            found.add(parse_date(row[column]))
        except (ValueError, IndexError) as exc:
            msg = f"{path.name} line {index + 1}: {exc}"
            raise ImportDataError(msg) from exc
    if not found:
        msg = f"{path.name} holds no expiry dates"
        raise ImportDataError(msg)
    return sorted(found)


# ------------------------------------------------------------------------------- importing


@dataclass(slots=True)
class ImportReport:
    dry_run: bool
    convention: str = ""
    spot_rows: int = 0
    spot_days: int = 0
    spot_first: date | None = None
    spot_last: date | None = None
    incomplete_spot_days: int = 0
    option_rows: int = 0
    option_contracts: int = 0
    option_day_pairs: int = 0  # (contract, day) combinations with candles
    expiries: int = 0
    bad_rows: int = 0
    skipped_other_instruments: int = 0
    dropped_out_of_session: int = 0
    notes: list[str] = field(default_factory=list)  # facts worth knowing
    warnings: list[str] = field(default_factory=list)  # things that can mislead a backtest


class CsvImporter:
    def __init__(
        self,
        store: Store,
        *,
        label: str = "csv",
        delimiter: str = ",",
        lenient: bool = False,
        batch_size: int = 100_000,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._label = label
        self._delimiter = delimiter
        self._lenient = lenient
        self._batch = batch_size
        self._say = progress or (lambda _text: None)

    def run(
        self,
        *,
        spot_files: Sequence[Path] = (),
        option_files: Sequence[Path] = (),
        expiries_file: Path | None = None,
        derive_expiries: bool = False,
        timestamps: TimestampMode = "auto",
        dry_run: bool = False,
    ) -> ImportReport:
        if not spot_files and not option_files:
            msg = "Nothing to import: give --spot and/or --options files."
            raise ImportDataError(msg)
        if expiries_file is not None and derive_expiries:
            msg = "Use either --expiries FILE or --derive-expiries, not both."
            raise ImportDataError(msg)
        if self._store.get_meta(DATA_SOURCE_KEY) == SYNTHETIC_SOURCE:
            msg = "This database holds simulated demo data; real data is never mixed into it."
            raise ImportDataError(msg)
        stored_expiries = self._store.load_expiries(UNDERLYING)
        if expiries_file is None and not derive_expiries and not stored_expiries:
            msg = (
                "The expiry dates are needed to choose each trade's contract. Pass "
                "--expiries FILE (one date per line), or --derive-expiries to use the expiries "
                "found in the option files (only right if they hold every weekly expiry)."
            )
            raise ImportDataError(msg)

        report = ImportReport(dry_run=dry_run)
        spot = self._read_spot(spot_files, report)
        contracts, option_days = self._validate_options(option_files, report)
        convention = self._decide_convention(spot, timestamps, report)
        report.convention = convention.value

        normalised = regular_session_only(to_start_convention(spot, convention)) if spot else []
        if spot:
            self._summarise_spot(spot, normalised, report)
        expiries = self._expiries(expiries_file, derive_expiries, contracts, report)
        self._check_coverage(
            normalised, contracts, option_days, expiries, stored_expiries, report, expiries_file
        )
        report.expiries = len(expiries)
        if dry_run:
            return report

        self._write(
            normalised, expiries, option_files, convention, report, wrote_spot=bool(spot_files)
        )
        return report

    # --------------------------------------------------------------------- reading

    def _read_spot(self, files: Sequence[Path], report: ImportReport) -> list[Candle]:
        by_start: dict[datetime, Candle] = {}
        duplicates = 0
        stats = ReadStats()
        for path in files:
            self._say(f"reading spot file {path.name}")
            for candle in iter_spot(
                path, delimiter=self._delimiter, lenient=self._lenient, stats=stats
            ):
                duplicates += candle.start in by_start
                by_start[candle.start] = candle
        self._absorb(stats, report, "spot")
        if duplicates:
            report.notes.append(f"{duplicates} repeated spot timestamps (the last row wins).")
        report.spot_rows = len(by_start)
        return [by_start[start] for start in sorted(by_start)]

    def _validate_options(
        self, files: Sequence[Path], report: ImportReport
    ) -> tuple[set[str], set[date]]:
        contracts: set[str] = set()
        days: set[date] = set()
        bad_ohlc = non_positive = rows = 0
        stats = ReadStats()
        for path in files:
            self._say(f"checking option file {path.name}")
            for symbol, candle in iter_options(
                path, delimiter=self._delimiter, lenient=self._lenient, stats=stats
            ):
                contracts.add(symbol)
                days.add(candle.start.date())
                rows += 1
                low, high = candle.low, candle.high
                if high < low or not (low <= candle.open <= high and low <= candle.close <= high):
                    bad_ohlc += 1
                if min(candle.open, high, low, candle.close) <= 0:
                    non_positive += 1
        self._absorb(stats, report, "option")
        report.option_rows = rows
        report.option_contracts = len(contracts)
        if bad_ohlc:
            report.warnings.append(
                f"{bad_ohlc} option candles have inconsistent open/high/low/close. They are kept "
                "as they are; a trade whose fill price is unusable is skipped and listed."
            )
        if non_positive:
            report.warnings.append(
                f"{non_positive} option candles have a price of zero. Kept; such fills are skipped."
            )
        return contracts, days

    def _absorb(self, stats: ReadStats, report: ImportReport, what: str) -> None:
        report.bad_rows += stats.bad_rows
        report.skipped_other_instruments += stats.other_instruments
        if stats.bad_rows:
            shown = "; ".join(stats.examples)
            report.warnings.append(
                f"{stats.bad_rows} unreadable {what} rows were skipped (--skip-bad-rows). "
                f"First ones: {shown}"
            )
        if stats.other_instruments:
            report.notes.append(
                f"{stats.other_instruments} {what} rows belong to another index and were skipped."
            )

    # ------------------------------------------------------------------ convention

    def _decide_convention(
        self, spot: Sequence[Candle], requested: TimestampMode, report: ImportReport
    ) -> Convention:
        stored_text = self._store.get_meta(CONVENTION_KEY)
        stored = Convention(stored_text) if stored_text else None
        seen = detect_convention(spot) if spot else Convention.UNKNOWN
        if requested != "auto":
            chosen = Convention(requested)
            if seen not in (Convention.UNKNOWN, chosen):
                msg = (
                    f"--timestamps {requested} contradicts the data: the index's first and last "
                    f"candle of each day say the stamps mark the {seen.value} of the minute."
                )
                raise ImportDataError(msg)
        elif seen is not Convention.UNKNOWN:
            chosen = seen
        elif stored is not None:
            chosen = stored
            report.notes.append(f"Timestamp convention '{chosen.value}' taken from earlier data.")
        else:
            msg = (
                "Cannot tell whether the timestamps mark the start or the end of each minute "
                "(the index's days do not begin 09:15/end 15:29 or begin 09:16/end 15:30). "
                "Refusing to guess, because a one-minute error would corrupt every signal. "
                "Pass --timestamps start or --timestamps close once you know."
            )
            raise ImportDataError(msg)
        if stored is not None and stored is not chosen:
            msg = (
                f"The data already stored uses the '{stored.value}' convention but these files "
                f"use '{chosen.value}'. Clear the stored data (make db-reset) before mixing."
            )
            raise ImportDataError(msg)
        if chosen is Convention.CLOSE:
            report.notes.append("Timestamps mark the END of each minute: shifted back one minute.")
        return chosen

    # ------------------------------------------------------------------- summaries

    def _summarise_spot(
        self, spot: Sequence[Candle], kept: Sequence[Candle], report: ImportReport
    ) -> None:
        by_day: dict[date, list[Candle]] = defaultdict(list)
        for candle in kept:
            by_day[candle.start.date()].append(candle)
        report.spot_days = len(by_day)
        if by_day:
            report.spot_first, report.spot_last = min(by_day), max(by_day)
        problems: list[str] = []
        for day in sorted(by_day):
            quality = assess_day(day, by_day[day])
            if not quality.complete:
                report.incomplete_spot_days += 1
            if not quality.clean:
                problems.append(f"{day}: {quality.describe()}")
        if report.incomplete_spot_days:
            report.warnings.append(
                f"{report.incomplete_spot_days} spot days do not have all 375 minutes and will be "
                "excluded from the backtest (and listed there). " + self._examples(problems)
            )
        report.dropped_out_of_session += len(spot) - len(kept)
        if len(spot) != len(kept):
            report.notes.append(
                f"{len(spot) - len(kept)} spot candles outside 09:15-15:29 were ignored."
            )

    @staticmethod
    def _examples(items: Sequence[str]) -> str:
        shown = "; ".join(items[:_MAX_LISTED])
        more = f" (+{len(items) - _MAX_LISTED} more)" if len(items) > _MAX_LISTED else ""
        return f"For example {shown}{more}." if items else ""

    def _expiries(
        self,
        path: Path | None,
        derive: bool,
        contracts: set[str],
        report: ImportReport,
    ) -> list[date]:
        if path is not None:
            return read_expiries(path, delimiter=self._delimiter)
        if not derive:
            return []
        derived = sorted({expiry_from_symbol(symbol) for symbol in contracts})
        if not derived:
            msg = "--derive-expiries found no option contracts to take expiries from."
            raise ImportDataError(msg)
        report.warnings.append(
            f"Expiry dates were derived from the option files ({len(derived)} dates). If a weekly "
            "expiry has no option data in your files, the app cannot know it exists and may trade "
            "a later expiry than the real nearest one."
        )
        return derived

    def _check_coverage(
        self,
        spot: Sequence[Candle],
        contracts: set[str],
        option_days: set[date],
        expiries: Sequence[date],
        stored: Sequence[date],
        report: ImportReport,
        expiries_file: Path | None,
    ) -> None:
        known = sorted({*expiries, *stored})
        complete_days = self._complete_days(spot)
        if complete_days and known:
            uncovered = [d for d in sorted(complete_days) if pick_expiry(d, known) is None]
            if uncovered:
                report.warnings.append(
                    f"{len(uncovered)} spot days have no expiry on or after them "
                    f"(from {uncovered[0]}): signals on those days will be skipped."
                )
        if expiries_file is not None and contracts:
            unlisted = sorted({e for e in map(expiry_from_symbol, contracts) if e not in known})
            if unlisted:
                report.warnings.append(
                    f"{len(unlisted)} contract expiries are not in the expiry list "
                    f"(for example {unlisted[0]}): those contracts will never be traded."
                )
        if complete_days and option_days:
            without = sorted(complete_days - option_days)
            if without:
                report.warnings.append(
                    f"{len(without)} complete spot days have no option candles at all "
                    f"(first: {without[0]}); trades on them will be skipped and listed."
                )
        elif complete_days:
            report.notes.append("This import has no option candles (only the index).")
        self._check_strikes(spot, contracts, report)

    @staticmethod
    def _complete_days(spot: Sequence[Candle]) -> set[date]:
        counts: dict[date, int] = defaultdict(int)
        for candle in spot:
            counts[candle.start.date()] += 1
        return {day for day, n in counts.items() if n == 375}

    @staticmethod
    def _check_strikes(spot: Sequence[Candle], contracts: set[str], report: ImportReport) -> None:
        if not spot or not contracts:
            return
        low = min(c.low for c in spot)
        high = max(c.high for c in spot)
        far = [
            symbol
            for symbol in contracts
            if not (low * Decimal("0.5") <= int(symbol.split("-")[3]) <= high * Decimal("1.5"))
        ]
        if far:
            report.warnings.append(
                f"{len(far)} option contracts have strikes far outside NIFTY's range in your spot "
                f"file (for example {sorted(far)[0]}). Does a file contain another index?"
            )

    # --------------------------------------------------------------------- writing

    def _write(
        self,
        spot: Sequence[Candle],
        expiries: Sequence[date],
        option_files: Sequence[Path],
        convention: Convention,
        report: ImportReport,
        *,
        wrote_spot: bool,
    ) -> None:
        store = self._store
        store.set_meta(CONVENTION_KEY, convention.value)
        if store.get_meta(DATA_SOURCE_KEY) is None:
            store.set_meta(DATA_SOURCE_KEY, CSV_SOURCE)
        tag = f"csv: {self._label}"

        if wrote_spot and spot:
            self._say(f"saving {len(spot)} spot candles")
            store.save_spot(SPOT_SYMBOL, spot)
            by_day: dict[date, list[Candle]] = defaultdict(list)
            for candle in spot:
                by_day[candle.start.date()].append(candle)
            entries: list[tuple[FetchKind, str, date, FetchStatus, int, str | None]] = []
            for day in sorted(by_day):
                quality: DayQuality = assess_day(day, by_day[day])
                note = quality.describe()
                entries.append(
                    (
                        "spot",
                        SPOT_SYMBOL,
                        day,
                        "ok",
                        quality.present,
                        f"{tag}; {note}" if note else tag,
                    )
                )
            store.log_fetch_many(entries)

        if expiries:
            store.save_expiries(UNDERLYING, expiries)
            months: dict[date, int] = defaultdict(int)
            for expiry in expiries:
                months[expiry.replace(day=1)] += 1
            store.log_fetch_many(
                [("expiries", UNDERLYING, m, "ok", n, tag) for m, n in sorted(months.items())]
            )

        if option_files:
            self._write_options(option_files, convention, report, tag)

    def _write_options(
        self, files: Sequence[Path], convention: Convention, report: ImportReport, tag: str
    ) -> None:
        store = self._store
        pending: list[tuple[str, Candle]] = []
        per_day: dict[tuple[str, date], int] = defaultdict(int)
        written = 0

        def flush() -> None:
            nonlocal written
            written += store.save_option_many(pending)
            self._say(f"saved {written} option candles")
            pending.clear()

        for path in files:
            self._say(f"importing option file {path.name}")
            for symbol, candle in iter_options(
                path, delimiter=self._delimiter, lenient=self._lenient
            ):
                if convention is Convention.CLOSE:
                    candle = replace(candle, start=candle.start - _ONE_MINUTE)
                if not in_session(candle.start):
                    report.dropped_out_of_session += 1
                    continue
                pending.append((symbol, candle))
                per_day[symbol, candle.start.date()] += 1
                if len(pending) >= self._batch:
                    flush()
        flush()
        report.option_rows = written
        report.option_day_pairs = len(per_day)
        store.log_fetch_many(
            [("option", symbol, day, "ok", n, tag) for (symbol, day), n in sorted(per_day.items())]
        )
