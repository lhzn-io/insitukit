"""Fetch observations and tide predictions from the NOAA CO-OPS data API (Tides and Currents).

Every request asks for metric units and GMT; conversion to the units we publish happens in the
builders. The API answers HTTP 200 with an ``{"error": ...}`` body for a bad request, and "No data
was found" when a station lacks the sensor or the window falls in a gap: that is an empty result,
not a failure. Server errors and timeouts retry with backoff.

Per-request range limits (checked against the live API, 2026-10):

- 6-minute observations (``water_temperature``, ``air_temperature``, ``wind``, ``air_pressure``,
  ``water_level``): 31 days. We ask for 30-day spans.
- the same products with ``interval=h`` (the reading at the top of each hour), and ``hourly_height``:
  365 days, compared by calendar date, so one calendar year (including a leap year) fits one request.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
META_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{station}.json"
USER_AGENT = "lhzn-blue-ingest/0.1 (+https://longhorizon.blue)"
APPLICATION = "lhzn-blue"
SIX_MINUTE_SPAN = timedelta(days=30)
# Value fields per product, renamed to the column names returned by fetch().
FIELDS = {
    "wind": {"s": "wind_speed", "d": "wind_direction", "g": "wind_gust"},
}
DATUM_PRODUCTS = {"water_level", "hourly_height", "high_low", "predictions"}

# The API sits behind a gateway that answers 403 "Forbidden" (not 429) once a client sends too many
# requests in a short period; a burst of about 300 requests in 10 minutes was enough. We pace every
# request and treat 403 and 429 as throttling, retried with backoff like a server error.
MIN_INTERVAL = 1.0
THROTTLED = {403, 429}

log = logging.getLogger(__name__)
_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_last_request = 0.0
# Requests sent since import (or since the caller reset it); used for logging and cost checks.
request_count = 0


class CoopsError(RuntimeError):
    """The API rejected the request (bad station, product or date range)."""


def _pace() -> None:
    """Keep at least MIN_INTERVAL seconds between requests."""
    global _last_request
    wait = _last_request + MIN_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _get(params: dict, retries: int = 4, timeout: int = 120) -> dict | None:
    """GET the data API with retries; return None when the API reports no data for the window."""
    global request_count
    query = {"units": "metric", "time_zone": "gmt", "format": "json", "application": APPLICATION, **params}
    delay = 5.0
    for attempt in range(1, retries + 1):
        request_count += 1
        _pace()
        try:
            resp = _session.get(BASE_URL, params=query, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("request failed (%s), attempt %d: %s", exc.__class__.__name__, attempt, params)
        else:
            if resp.status_code == 200:
                body = resp.json()
                err = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
                if err is None:
                    return body
                if "no data was found" in err.lower():
                    return None
                raise CoopsError(f"{err.strip()} ({params})")
            if resp.status_code < 500 and resp.status_code not in THROTTLED:
                raise CoopsError(f"HTTP {resp.status_code} ({params})")
            log.warning("HTTP %d, attempt %d: %s", resp.status_code, attempt, params)
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"giving up after {retries} attempts: {params}")


def _stamp(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y%m%d %H:%M")


def spans(start: datetime, end: datetime, hourly: bool) -> list[tuple[datetime, datetime]]:
    """Request windows covering ``[start, end)``: calendar years for hourly data, 30 days otherwise."""
    out = []
    lo = start
    while lo < end:
        if hourly:
            hi = min(datetime(lo.year + 1, 1, 1, tzinfo=timezone.utc), end)
        else:
            hi = min(lo + SIX_MINUTE_SPAN, end)
        out.append((lo, hi))
        lo = hi
    return out


def fetch(
    product: str,
    station: str,
    start: datetime,
    end: datetime,
    interval: str | None = None,
    datum: str = "MLLW",
) -> pd.DataFrame:
    """Observations for ``start <= time < end`` with a UTC ``time`` column and float value columns.

    Single-value products return one column named after the product; ``wind`` returns
    ``wind_speed`` (m/s), ``wind_direction`` (degrees true) and ``wind_gust`` (m/s). Readings the
    API flags as missing come back as NaN. Long windows are split to respect the range limits.
    """
    fields = FIELDS.get(product, {"v": product})
    hourly = interval == "h" or product == "hourly_height"
    frames = []
    for lo, hi in spans(start, end, hourly):
        # The API's end_date is inclusive; stop one minute short so adjacent spans do not overlap.
        params = {"product": product, "station": station, "begin_date": _stamp(lo), "end_date": _stamp(hi - timedelta(minutes=1))}
        if interval:
            params["interval"] = interval
        if product in DATUM_PRODUCTS:
            params["datum"] = datum
        body = _get(params)
        rows = (body or {}).get("data") or []
        log.info("%s %s %s..%s: %d rows", station, product, params["begin_date"], params["end_date"], len(rows))
        if rows:
            frames.append(pd.DataFrame(rows))
    cols = ["time", *fields.values()]
    if not frames:
        return pd.DataFrame({c: pd.Series(dtype="datetime64[ns, UTC]" if c == "time" else float) for c in cols})
    raw = pd.concat(frames, ignore_index=True)
    df = pd.DataFrame({"time": pd.to_datetime(raw["t"], utc=True)})
    for key, name in fields.items():
        df[name] = pd.to_numeric(raw[key], errors="coerce") if key in raw.columns else np.nan
    df = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] < pd.Timestamp(end))]
    return df.sort_values("time").drop_duplicates("time").reset_index(drop=True)


def tide_events(station: str, start: datetime, end: datetime, datum: str = "MLLW") -> list[dict]:
    """High and low tide predictions in ``[start, end)``: ``[{"time": Timestamp, "m": float, "type": "H"|"L"}]``."""
    params = {
        "product": "predictions",
        "station": station,
        "begin_date": _stamp(start),
        "end_date": _stamp(end - timedelta(minutes=1)),
        "interval": "hilo",
        "datum": datum,
    }
    body = _get(params) or {}
    out = []
    for row in body.get("predictions") or []:
        try:
            out.append({"time": pd.Timestamp(row["t"], tz="UTC"), "m": float(row["v"]), "type": row["type"]})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def metadata(station: str) -> dict:
    """Station metadata with details and sensors (name, lat, lng, established, sensor list)."""
    global request_count
    request_count += 1
    _pace()
    resp = _session.get(META_URL.format(station=station), params={"expand": "details,sensors"}, timeout=60)
    resp.raise_for_status()
    return resp.json()["stations"][0]
