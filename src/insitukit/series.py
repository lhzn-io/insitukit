"""The compact JSON form of a regular time series.

One series::

    {"t0": "2026-10-01T00:00:00Z", "step": 3600, "unit": "m", "values": [0.41, 0.44, null, 0.52]}

Several series on one grid (a frame)::

    {"t0": "2026-10-01T00:00:00Z", "step": 3600, "wind_kt": [8.1, 9.4], "dir_deg": [215.0, 220.0]}

``t0`` is the first value's time in UTC, ``step`` the spacing in seconds, and a missing value is ``null``. The grid
runs without a break from the first value to the last, so a reader rebuilds every time from ``t0``, ``step`` and the
position alone. An empty series has ``t0`` null. Values are rounded on the way out, which with the implicit times
keeps a year of hourly data to a few tens of kilobytes.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd


def iso(ts: datetime | pd.Timestamp) -> str:
    """A time as ``YYYY-MM-DDTHH:MM:SSZ`` in UTC."""
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _grid(index: pd.DatetimeIndex, step: int) -> pd.DatetimeIndex:
    return pd.date_range(index[0], index[-1], freq=f"{step}s")


def _rounded(values, places: int) -> list[float | None]:
    return [None if pd.isna(v) else round(float(v), places) for v in values]


def encode(s: pd.Series, unit: str, places: int, step: int = 3600) -> dict:
    """A series on a UTC grid of ``step`` seconds (indexed on that grid already, gaps allowed) as JSON."""
    s = s.dropna()
    if s.empty:
        return {"t0": None, "step": step, "unit": unit, "values": []}
    full = s.reindex(_grid(s.index, step))
    return {"t0": iso(full.index[0]), "step": step, "unit": unit, "values": _rounded(full, places)}


def decode(obj: dict | None) -> pd.Series:
    """The values of an encoded series that are present, indexed by UTC time."""
    if not obj or not obj.get("t0") or not obj.get("values"):
        return pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
    idx = pd.date_range(pd.Timestamp(obj["t0"]), periods=len(obj["values"]), freq=f"{obj.get('step', 3600)}s")
    return pd.Series([np.nan if v is None else v for v in obj["values"]], index=idx, dtype=float).dropna()


def encode_frame(frame: pd.DataFrame, places: dict[str, int] | int = 1, step: int = 3600) -> dict:
    """Columns on one UTC grid of ``step`` seconds as JSON, each rounded to ``places`` (one number, or per column
    with 1 for any not named)."""
    frame = frame.dropna(how="all")
    if frame.empty:
        return {"t0": None, "step": step}
    full = frame.reindex(_grid(frame.index, step))
    out: dict = {"t0": iso(full.index[0]), "step": step}
    for col in full.columns:
        p = places if isinstance(places, int) else places.get(col, 1)
        out[col] = _rounded(full[col], p)
    return out


def decode_frame(obj: dict | None, skip: tuple[str, ...] = ()) -> pd.DataFrame:
    """The rows of an encoded frame with any value present. Every list is a column except those named in ``skip``
    (lists a writer added beside the columns, such as indices of flagged values)."""
    if not obj or not obj.get("t0"):
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"), dtype=float)
    cols = [k for k, v in obj.items() if isinstance(v, list) and k not in skip]
    if not cols:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"), dtype=float)
    idx = pd.date_range(pd.Timestamp(obj["t0"]), periods=len(obj[cols[0]]), freq=f"{obj.get('step', 3600)}s")
    return pd.DataFrame({c: pd.to_numeric(pd.Series(obj[c], index=idx), errors="coerce") for c in cols}).dropna(how="all")
