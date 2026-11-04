"""Where the stored candles came from, in words the pages can show.

The importer records its ``--label`` in the fetch log (``csv: huggingface``), so the answer can be
read back without extra bookkeeping. Nothing here is guessed: an unknown label is shown as given.
"""

from typing import Any

from app.history.csv_import import CSV_SOURCE
from app.history.synthetic import SYNTHETIC_SOURCE

HUGGING_FACE_URL = "https://huggingface.co/datasets/thetrademarkk/india-index-options-1m"

_HUGGING_FACE: dict[str, Any] = {
    "kind": "huggingface",
    "title": "Hugging Face dataset thetrademarkk/india-index-options-1m",
    "url": HUGGING_FACE_URL,
    "note": (
        "Free community data: NIFTY 1-minute index and weekly option candles. "
        "It is for educational use and has not been checked against NSE."
    ),
}


def describe_source(source: str | None, labels: list[str]) -> dict[str, Any] | None:
    """What to tell the user about the data. ``None`` when there is no data or it is simulated."""
    if source is None or source == SYNTHETIC_SOURCE:
        return None
    if source == CSV_SOURCE:
        if any(label.lower() == "huggingface" for label in labels):
            return dict(_HUGGING_FACE)
        shown = ", ".join(labels) if labels else "files"
        return {
            "kind": "csv",
            "title": f"CSV files you imported ({shown})",
            "url": None,
            "note": "How accurate this is depends on the provider of those files.",
        }
    return {
        "kind": "broker",
        "title": "Historical candles downloaded from your broker",
        "url": None,
        "note": "",
    }
