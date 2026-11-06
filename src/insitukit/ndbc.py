"""NDBC buoy observations: standard meteorology (wind, air and sea temperature, pressure, waves) and ADCP currents.

Files come from the NDBC data server in the order it publishes them: the per-month file for months of the current
year (``data/<product>/<Mon>/<id><m><yyyy>.txt.gz``), the yearly historical file
(``data/historical/<product>/<id>h<yyyy>.txt.gz``), then the rolling 45-day real-time file
(``data/realtime2/<ID>.txt`` or ``.adcp``). ``fetch`` takes each month from the first of those that has it.

Values are as NDBC publishes them (``df.attrs["units"]`` has the units row of the file); ``MM`` and each column's own
missing-value sentinel (99, 999 or 9999, see ``SENTINELS``) become NaN. Times are UTC, in a ``time`` column. ``currents`` gives the first ADCP bin as
speed in m/s (NDBC reports cm/s) and eastward and northward components.

Requests are paced (``MIN_INTERVAL`` apart, across threads), retried on server errors and timeouts, and carry the
User-Agent set with ``insitukit.identify``. A 404 is a file NDBC does not have, not an error. Every function takes an
optional ``get`` (a URL to text, or None for a missing file) so callers can test without the network.
"""

from __future__ import annotations

import gzip
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime

import numpy as np
import pandas as pd
import requests

from . import coops

log = logging.getLogger(__name__)

NDBC_URL = "https://www.ndbc.noaa.gov"
PRODUCTS = ("stdmet", "adcp")
REALTIME_SUFFIX = {"stdmet": "txt", "adcp": "adcp"}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# NDBC's "not measured" value for each standard meteorological column. They differ by column because a value that
# means "missing" in one is a real reading in another: 999.0 hPa is a deep low, 99 degrees a wind direction.
SENTINELS = {"WDIR": 999.0, "MWD": 999.0, "WSPD": 99.0, "GST": 99.0, "WVHT": 99.0, "DPD": 99.0, "APD": 99.0,
             "PRES": 9999.0, "ATMP": 999.0, "WTMP": 999.0, "DEWP": 999.0, "VIS": 99.0, "PTDY": 99.0, "TIDE": 99.0}
# ADCP bins (DEPnn, DIRnn, SPDnn: m, degrees, cm/s), where 99 is a real depth, direction or speed.
ADCP_SENTINELS = (999.0, 9999.0)
# Any other column.
OTHER_SENTINELS = (99.0, 999.0, 9999.0)


def _missing(column: str):
    if column in SENTINELS:
        return (SENTINELS[column],)
    if re.fullmatch(r"(DEP|DIR|SPD)\d\d", column):
        return ADCP_SENTINELS
    return OTHER_SENTINELS
# Column names of files from before 2007, as later files name them.
RENAMED = {"YYYY": "YY", "WD": "WDIR", "BAR": "PRES"}
MIN_INTERVAL = 0.5
RETRIES = 3

Getter = Callable[[str], "str | None"]

_lock = threading.Lock()
_last_request = 0.0
_session = requests.Session()
# Requests sent since import; used for logging and cost checks.
request_count = 0


def _pace() -> None:
    global _last_request, request_count
    with _lock:
        request_count += 1
        wait = _last_request + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def _get(url: str, timeout: int = 120) -> str | None:
    """The file at ``url`` as text (gunzipped for ``.gz``); None when NDBC does not have it."""
    delay = 5.0
    for attempt in range(1, RETRIES + 1):
        _pace()
        try:
            resp = _session.get(url, timeout=timeout, headers={"User-Agent": coops.USER_AGENT})
        except requests.RequestException as exc:
            log.warning("request failed (%s), attempt %d: %s", exc.__class__.__name__, attempt, url)
        else:
            if resp.status_code == 404:
                return None
            if resp.status_code == 200:
                if url.endswith(".gz"):
                    return gzip.decompress(resp.content).decode("utf-8", errors="replace")
                return resp.text
            if resp.status_code < 500 and resp.status_code != 429:
                raise RuntimeError(f"HTTP {resp.status_code}: {url}")
            log.warning("HTTP %d, attempt %d: %s", resp.status_code, attempt, url)
        if attempt < RETRIES:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"giving up after {RETRIES} attempts: {url}")


def month_code(month: int) -> str:
    """NDBC's month code in per-month file names: 1 to 9, then a, b and c for October to December."""
    return str(month) if month < 10 else "abc"[month - 10]


def urls(station: str, product: str, year: int, month: int) -> list[str]:
    """The files that may hold ``product`` for ``station`` in a month, in the order to try them."""
    if product not in PRODUCTS:
        raise ValueError(f"unknown NDBC product {product!r}; one of {PRODUCTS}")
    sid = station.lower()
    return [
        f"{NDBC_URL}/data/{product}/{MONTHS[month - 1]}/{sid}{month_code(month)}{year}.txt.gz",
        f"{NDBC_URL}/data/historical/{product}/{sid}h{year}.txt.gz",
        f"{NDBC_URL}/data/realtime2/{station.upper()}.{REALTIME_SUFFIX[product]}",
    ]


def parse(text: str) -> pd.DataFrame:
    """An NDBC whitespace-delimited file as a frame: a UTC ``time`` column and float columns, NaN where NDBC writes a
    sentinel or ``MM``. Handles four- and two-digit years, files with and without a minute column, and the column
    names of files from before 2007 (``WD``, ``BAR``)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("#") and not lines[0].startswith("YY"):
        raise ValueError("not an NDBC data file: no header row")
    names = [RENAMED.get(n, n) for n in lines[0].lstrip("#").split()]
    unit_row = lines[1].lstrip("#").split() if len(lines) > 1 and lines[1].startswith("#") else []
    rows = [ln.split() for ln in lines[1:] if not ln.startswith("#")]
    width = len(rows[0]) if rows else len(names)
    df = pd.DataFrame(rows, columns=names[:width])
    if df.empty:
        out = pd.DataFrame({"time": pd.Series(dtype="datetime64[ns, UTC]")})
    else:
        years = df["YY"].astype(int)
        years = years.where(years >= 100, years + 1900)
        minutes = df["mm"].astype(int) if "mm" in df else 0
        stamp = pd.to_datetime(dict(year=years, month=df["MM"].astype(int), day=df["DD"].astype(int),
                                    hour=df["hh"].astype(int), minute=minutes), utc=True)
        values = df.drop(columns=[c for c in ("YY", "MM", "DD", "hh", "mm") if c in df])
        values = values.replace("MM", np.nan).apply(pd.to_numeric, errors="coerce")
        for col in values.columns:
            values[col] = values[col].mask(values[col].isin(_missing(col)))
        out = pd.concat([pd.DataFrame({"time": stamp}), values], axis=1)
    out.attrs["units"] = {n: u for n, u in zip(names, unit_row) if n not in ("YY", "MM", "DD", "hh", "mm")}
    return out


def _empty() -> pd.DataFrame:
    return pd.DataFrame({"time": pd.Series(dtype="datetime64[ns, UTC]")})


def fetch(station: str, start: datetime, end: datetime, product: str = "stdmet",
          get: Getter | None = None) -> pd.DataFrame:
    """``product`` (``stdmet`` or ``adcp``) at ``station`` for ``start <= time < end``, each month from the first file
    that has it; each file is downloaded once. ``df.attrs["sources"]`` lists the files used."""
    if product not in PRODUCTS:
        raise ValueError(f"unknown NDBC product {product!r}; one of {PRODUCTS}")
    get = get or _get
    t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
    t0 = t0.tz_localize("UTC") if t0.tzinfo is None else t0.tz_convert("UTC")
    t1 = t1.tz_localize("UTC") if t1.tzinfo is None else t1.tz_convert("UTC")
    texts: dict[str, str | None] = {}
    frames, sources, units = [], [], {}
    first = t0.tz_localize(None).normalize().replace(day=1)
    for month in pd.date_range(first, t1.tz_localize(None), freq="MS"):
        lo = max(t0, month.tz_localize("UTC"))
        hi = min(t1, (month + pd.offsets.MonthBegin(1)).tz_localize("UTC"))
        if lo >= hi:
            continue
        for url in urls(station, product, month.year, month.month):
            if url not in texts:
                texts[url] = get(url)
            if texts[url] is None:
                continue
            df = parse(texts[url])
            units = units or df.attrs.get("units", {})
            df = df[(df["time"] >= lo) & (df["time"] < hi)]
            if len(df):
                frames.append(df)
                if url not in sources:
                    sources.append(url)
                break
    if not frames:
        out = _empty()
    else:
        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    out.attrs = {"sources": sources, "units": units}
    return out


def year(station: str, year: int, product: str = "stdmet", get: Getter | None = None) -> pd.DataFrame | None:
    """One year's historical file of ``product`` at ``station``; None when NDBC has none for that year."""
    if product not in PRODUCTS:
        raise ValueError(f"unknown NDBC product {product!r}; one of {PRODUCTS}")
    text = (get or _get)(f"{NDBC_URL}/data/historical/{product}/{station.lower()}h{year}.txt.gz")
    return None if text is None else parse(text)


def currents(station: str, start: datetime, end: datetime, get: Getter | None = None) -> pd.DataFrame:
    """The first ADCP bin for ``start <= time < end``: ``time``, ``depth`` (m), ``speed`` (m/s), ``direction`` (degrees
    true, toward), and ``u`` and ``v`` (m/s, east and north)."""
    df = fetch(station, start, end, "adcp", get=get)
    out = pd.DataFrame({"time": df["time"]})
    speed = df["SPD01"] / 100.0 if "SPD01" in df else pd.Series(np.nan, index=df.index)
    toward = df["DIR01"] if "DIR01" in df else pd.Series(np.nan, index=df.index)
    out["depth"] = df["DEP01"] if "DEP01" in df else np.nan
    out["speed"], out["direction"] = speed, toward
    out["u"], out["v"] = speed * np.sin(np.deg2rad(toward)), speed * np.cos(np.deg2rad(toward))
    out.attrs = df.attrs
    return out


def position(station: str, get: Getter | None = None) -> tuple[float, float]:
    """(lat, lon) of a station, from its NDBC station page."""
    html = (get or _get)(f"{NDBC_URL}/station_page.php?station={station.lower()}") or ""
    m = re.search(r"(\d+\.\d+)\s*([NS])\s+(\d+\.\d+)\s*([EW])", html)
    if not m:
        raise ValueError(f"no position found for NDBC station {station}")
    lat = float(m.group(1)) * (1 if m.group(2) == "N" else -1)
    lon = float(m.group(3)) * (1 if m.group(4) == "E" else -1)
    return lat, lon
