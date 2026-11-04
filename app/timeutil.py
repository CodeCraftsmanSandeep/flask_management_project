"""Time zone helpers. The whole app works in naive IST wall-clock times (NSE's own clock)."""

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def to_naive_ist(moment: datetime) -> datetime:
    """Wall-clock IST for *moment*; naive inputs are assumed to be IST already."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(IST).replace(tzinfo=None)
