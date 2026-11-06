"""Fetch observations, predictions and station metadata from the NOAA CO-OPS APIs (Tides and Currents).

Every data request asks for metric units and GMT unless the caller chooses English units where offered; converting
to other units is the caller's. Current speeds are converted from the cm/s the API reports (in either unit system)
to m/s. The data API answers an HTTP 200 or 400 with an ``{"error": ...}`` body for a bad request, and "No data was
found" when a station lacks the sensor or the window falls in a gap: that is an empty result, not a failure. A request
for a datum the station does not publish raises ``DatumUnavailable``, so a caller can fall back to another. Server
errors and timeouts retry with backoff.

Per-request range limits (checked against the live API, 2026-10):

- 6-minute observations (``water_temperature``, ``air_temperature``, ``wind``, ``air_pressure``,
  ``water_level``, ``currents``): 31 days. We ask for 30-day spans.
- the same products with ``interval=h`` (the reading at the top of each hour), and ``hourly_height``:
  365 days, compared by calendar date, so one calendar year (including a leap year) fits one request.

Requests carry ``USER_AGENT`` and ``APPLICATION``; set your own with ``insitukit.identify``. Requests from several
threads share one pacing clock.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from . import __version__

BASE_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
MDAPI_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi"
META_URL = MDAPI_URL + "/stations/{station}.json"
USER_AGENT = f"insitukit/{__version__} (+https://github.com/lhzn-io/insitukit)"
APPLICATION = "insitukit"
SIX_MINUTE_SPAN = timedelta(days=30)
# Value fields per product, renamed to the column names returned by fetch().
FIELDS = {
    "wind": {"s": "wind_speed", "d": "wind_direction", "g": "wind_gust"},
    "currents": {"s": "speed", "d": "direction", "b": "bin"},
}
# Columns the API reports in cm/s, whatever the units requested; fetch() returns them in m/s.
CM_PER_S = {("currents", "speed")}
DATUM_PRODUCTS = {"water_level", "hourly_height", "high_low", "predictions"}
FLOOD_LEVELS = ("nos_minor", "nos_moderate", "nos_major", "nws_minor", "nws_moderate", "nws_major")

# The API sits behind a gateway that answers 403 "Forbidden" (not 429) once a client sends too many
# requests in a short period; a burst of about 300 requests in 10 minutes was enough. We pace every
# request and treat 403 and 429 as throttling, retried with backoff like a server error.
MIN_INTERVAL = 1.0
THROTTLED = {403, 429}

log = logging.getLogger(__name__)
_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_lock = threading.Lock()
_last_request = 0.0
# Requests sent since import (or since the caller reset it); used for logging and cost checks.
request_count = 0


class CoopsError(RuntimeError):
    """The API rejected the request (bad station, product or date range)."""


class DatumUnavailable(CoopsError):
    """The station does not publish the datum asked for."""


def _pace() -> None:
    """Count the request and keep at least MIN_INTERVAL seconds between requests, across threads."""
    global _last_request, request_count
    with _lock:
        request_count += 1
        wait = _last_request + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def _error_message(resp: requests.Response) -> str | None:
    """The message of an ``{"error": {"message": ...}}`` body, or None."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
        return None
    return str(body["error"].get("message") or "").strip() or None


def _request(url: str, params: dict, retries: int, timeout: int) -> dict | None:
    """GET JSON with pacing and retries; None when the API reports no data for the window."""
    delay = 5.0
    for attempt in range(1, retries + 1):
        _pace()
        try:
            resp = _session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("request failed (%s), attempt %d: %s", exc.__class__.__name__, attempt, params)
        else:
            if resp.status_code < 500 and resp.status_code not in THROTTLED:
                err = _error_message(resp)
                if err is not None:
                    if "no data was found" in err.lower():
                        return None
                    cls = DatumUnavailable if "datum" in err.lower() else CoopsError
                    raise cls(f"{err} ({params})")
                if resp.status_code == 200:
                    return resp.json()
                raise CoopsError(f"HTTP {resp.status_code} ({params})")
            log.warning("HTTP %d, attempt %d: %s", resp.status_code, attempt, params)
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"giving up after {retries} attempts: {params}")


def _get(params: dict, retries: int = 4, timeout: int = 120) -> dict | None:
    """GET the data API with retries; return None when the API reports no data for the window."""
    query = {"units": "metric", "time_zone": "gmt", "format": "json", "application": APPLICATION, **params}
    return _request(BASE_URL, query, retries, timeout)


def _md(path: str, params: dict | None = None, retries: int = 3, timeout: int = 120) -> dict:
    """GET the metadata API: ``path`` below ``.../webapi``, for example ``stations/8461490/datums.json``."""
    body = _request(f"{MDAPI_URL}/{path}", params or {}, retries, timeout)
    if body is None:
        raise CoopsError(f"no data: {path} ({params})")
    return body


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


def _window(lo: datetime, hi: datetime) -> dict:
    # The API's end_date is inclusive; stop one minute short so adjacent spans do not overlap.
    return {"begin_date": _stamp(lo), "end_date": _stamp(hi - timedelta(minutes=1))}


def _in_range(df: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    df = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] < pd.Timestamp(end))]
    return df.sort_values("time").drop_duplicates("time").reset_index(drop=True)


def _empty(cols: list[str], text: tuple[str, ...] = ()) -> pd.DataFrame:
    def dtype(c: str):
        return "datetime64[ns, UTC]" if c == "time" else (object if c in text else float)

    return pd.DataFrame({c: pd.Series(dtype=dtype(c)) for c in cols})


def fetch(
    product: str,
    station: str,
    start: datetime,
    end: datetime,
    interval: str | None = None,
    datum: str = "MLLW",
    bin: int | None = None,
) -> pd.DataFrame:
    """Observations for ``start <= time < end`` with a UTC ``time`` column and float value columns.

    Single-value products return one column named after the product; ``wind`` returns ``wind_speed`` (m/s),
    ``wind_direction`` (degrees true) and ``wind_gust`` (m/s); ``currents`` returns ``speed`` (m/s), ``direction``
    (degrees true, the direction of flow) and ``bin``, for ``bin`` or, when None, the station's default bin.
    Readings the API flags as missing come back as NaN. Long windows are split to respect the range limits.
    """
    fields = FIELDS.get(product, {"v": product})
    hourly = interval == "h" or product == "hourly_height"
    frames = []
    for lo, hi in spans(start, end, hourly):
        params = {"product": product, "station": station, **_window(lo, hi)}
        if interval:
            params["interval"] = interval
        if product in DATUM_PRODUCTS:
            params["datum"] = datum
        if bin is not None:
            params["bin"] = bin
        body = _get(params)
        rows = (body or {}).get("data") or []
        log.info("%s %s %s..%s: %d rows", station, product, params["begin_date"], params["end_date"], len(rows))
        if rows:
            frames.append(pd.DataFrame(rows))
    cols = ["time", *fields.values()]
    if not frames:
        return _empty(cols)
    raw = pd.concat(frames, ignore_index=True)
    df = pd.DataFrame({"time": pd.to_datetime(raw["t"], utc=True)})
    for key, name in fields.items():
        df[name] = pd.to_numeric(raw[key], errors="coerce").astype(float) if key in raw.columns else np.nan
        if (product, name) in CM_PER_S:
            df[name] = df[name] / 100.0
    return _in_range(df, start, end)


def tide_events(station: str, start: datetime, end: datetime, datum: str = "MLLW", units: str = "metric") -> list[dict]:
    """High and low tide predictions in ``[start, end)``: ``[{"time": Timestamp, "m": float, "type": "H"|"L"}]``.
    The height is in metres, or in feet (still under ``"m"``) with ``units="english"``."""
    params = {
        "product": "predictions",
        "station": station,
        "begin_date": _stamp(start),
        "end_date": _stamp(end - timedelta(minutes=1)),
        "interval": "hilo",
        "datum": datum,
        "units": units,
    }
    body = _get(params) or {}
    out = []
    for row in body.get("predictions") or []:
        try:
            out.append({"time": pd.Timestamp(row["t"], tz="UTC"), "m": float(row["v"]), "type": row["type"]})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def predictions(
    station: str, start: datetime, end: datetime, datum: str = "MLLW", interval: str = "6", units: str = "metric"
) -> pd.Series:
    """Tide predictions above ``datum`` in ``[start, end)``, indexed by UTC time: every 6 minutes by default, or at the
    top of each hour with ``interval="h"``; metres, or feet with ``units="english"``. A station's own predictions
    taken from its observed water level give the surge without the phase shift of a nearby station's."""
    vals: dict[pd.Timestamp, float] = {}
    for lo, hi in spans(start, end, hourly=interval == "h"):
        body = _get({"product": "predictions", "station": station, "datum": datum, "interval": interval,
                     "units": units, **_window(lo, hi)}) or {}
        for row in body.get("predictions") or []:
            try:
                vals[pd.Timestamp(row["t"], tz="UTC")] = float(row["v"])
            except (KeyError, TypeError, ValueError):
                continue
    s = pd.Series(vals, dtype=float).sort_index()
    return s[(s.index >= pd.Timestamp(start)) & (s.index < pd.Timestamp(end))]


def current_predictions(
    station: str,
    start: datetime,
    end: datetime,
    bin: int | None = None,
    interval: int | str = 30,
    vel_type: str | None = None,
) -> pd.DataFrame:
    """Tidal current predictions in ``[start, end)``: a UTC ``time`` column and ``velocity_major``, the speed along
    the flood axis in m/s, positive on the flood.

    ``interval`` is minutes (1, 6, 10, 30, 60) or ``"MAX_SLACK"`` for the maximum flood, maximum ebb and slack
    events, which adds a ``type`` column (``flood``, ``ebb`` or ``slack``). ``bin`` defaults to the station's own
    prediction bin. ``attrs`` carries ``bin``, ``depth_m``, ``flood_dir_deg`` and ``ebb_dir_deg`` (degrees true).
    A subordinate station (predicted by offsets from a reference station) publishes events only, so any other
    interval raises ``CoopsError``.
    """
    events = str(interval).upper() == "MAX_SLACK"
    rows: list[dict] = []
    for lo, hi in spans(start, end, hourly=False):
        params = {"product": "currents_predictions", "station": station, "interval": str(interval), **_window(lo, hi)}
        if bin is not None:
            params["bin"] = bin
        if vel_type:
            params["vel_type"] = vel_type
        body = _get(params) or {}
        cp = (body.get("current_predictions") or {}).get("cp") or []
        rows.extend([cp] if isinstance(cp, dict) else cp)  # a window with one event comes back unwrapped
    cols = ["time", "velocity_major", *(["type"] if events else [])]
    if not rows:
        return _empty(cols, text=("type",))
    if not events and any("Type" in r for r in rows):
        raise CoopsError(f"{station} is a subordinate current station: it publishes MAX_SLACK events only")
    df = pd.DataFrame({
        "time": pd.to_datetime([r.get("Time") for r in rows], utc=True),
        "velocity_major": pd.to_numeric(pd.Series([r.get("Velocity_Major") for r in rows]), errors="coerce") / 100.0,
    })
    if events:
        df["type"] = [r.get("Type") for r in rows]
    first = rows[0]

    def num(key: str) -> float | None:
        try:
            return float(first[key])
        except (KeyError, TypeError, ValueError):
            return None

    df = _in_range(df, start, end)
    bin_no = num("Bin")
    df.attrs = {"bin": None if bin_no is None else int(bin_no), "depth_m": num("Depth"),
                "flood_dir_deg": num("meanFloodDir"), "ebb_dir_deg": num("meanEbbDir")}
    return df


def metadata(station: str, expand: str = "details,sensors") -> dict:
    """Station metadata: name, lat, lng and the ``expand`` resources (``details,sensors`` by default, or
    ``deployments,bins`` for a current meter)."""
    return _md(f"stations/{station}.json", {"expand": expand} if expand else None)["stations"][0]


def datums(station: str, units: str = "metric") -> dict:
    """Tidal datums on the station's current epoch: ``{"datums": {name: value}, "epoch", "units", "orthometric"}``.
    Values are heights above the station datum (STND) in metres, or feet with ``units="english"``; subtract
    ``datums["MLLW"]`` for heights above MLLW."""
    body = _md(f"stations/{station}/datums.json", {"units": units})
    values = {d["name"]: float(d["value"]) for d in body.get("datums") or []
              if d.get("name") and isinstance(d.get("value"), (int, float))}
    return {"datums": values, "epoch": body.get("epoch") or None, "units": body.get("units"),
            "orthometric": body.get("OrthometricDatum") or None}


def flood_levels(station: str, units: str = "metric") -> dict[str, float | None]:
    """NOS and NWS minor, moderate and major flood thresholds above the station datum (STND), in metres or feet; a
    threshold NOAA does not publish is None."""
    body = _md(f"stations/{station}/floodlevels.json", {"units": units})
    return {k: (float(body[k]) if isinstance(body.get(k), (int, float)) else None) for k in FLOOD_LEVELS}


def harcon(station: str, bin: int | None = None, units: str = "metric") -> list[dict]:
    """Harmonic constituents as published. For a tide station each has ``name``, ``amplitude``, ``phase_GMT`` and
    ``speed``; for a current station there is one per bin and constituent (``binNbr``, ``constituentName``,
    ``majorAmplitude`` in cm/s, ``majorPhaseGMT``, ``azi``), kept to ``bin`` when given."""
    params: dict = {"units": units}
    if bin is not None:
        params["bin"] = bin
    rows = _md(f"stations/{station}/harcon.json", params).get("HarmonicConstituents") or []
    if bin is not None:
        rows = [r for r in rows if r.get("binNbr", bin) == bin]
    return rows


def stations(
    type: str | None = None, bbox: tuple[float, float, float, float] | None = None, units: str | None = "metric"
) -> list[dict]:
    """The station list, of one ``type`` when given (for example ``waterlevels``, ``currents``,
    ``currentpredictions`` or ``tidepredictions``), within ``bbox`` = (min_lon, min_lat, max_lon, max_lat) when
    given. Each station is the API's dict, with ``id``, ``name``, ``lat`` and ``lng`` among its keys; depths are in
    metres unless ``units`` says otherwise."""
    params = {k: v for k, v in {"type": type, "units": units}.items() if v}
    out = _md("stations.json", params or None).get("stations") or []
    if bbox is None:
        return out
    lo_lon, lo_lat, hi_lon, hi_lat = bbox

    def inside(s: dict) -> bool:
        try:
            lat, lon = float(s["lat"]), float(s["lng"])
        except (KeyError, TypeError, ValueError):
            return False
        return lo_lon <= lon <= hi_lon and lo_lat <= lat <= hi_lat

    return [s for s in out if inside(s)]
