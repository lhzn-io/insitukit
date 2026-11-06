"""A small ERDDAP tabledap client, for batch jobs and for long-running services.

Requests start at least ``min_interval`` apart, on any number of threads; each attempt counts against a per-instance
budget; a 503 or 429 with Retry-After is waited out; and a 404 whose body says "no matching results" is an empty
window, not a missing dataset; any other 404 raises ``DatasetMissing``.

Failures are remembered so later requests fail at once instead of waiting out the same timeout again:

- a host that refuses or does not accept a connection on every attempt is recorded, and every request to it is
  skipped (``Unreachable``);
- with a ``failure_ttl``, a dataset whose read timed out or that answered with a 5xx is recorded too, and its requests
  are skipped (``DatasetUnavailable``).

With ``failure_ttl=None`` (the default, for batch runs) a host stays recorded for the life of the instance and
datasets are not recorded, as in 0.2. A number of seconds makes both expire, for services that keep one client for
days; 0 records nothing. Several instances can share one ``FailureCache``.
"""

from __future__ import annotations

import io
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlsplit

import pandas as pd
import requests

from . import coops

log = logging.getLogger(__name__)

NERACOOS_URL = "https://data.neracoos.org/erddap"


class Unavailable(RuntimeError):
    """A request was not answered with data: the host or dataset failed now, or recently and was skipped."""

    def __init__(self, message: str, url: str | None = None):
        super().__init__(message)
        self.url = url


class Unreachable(Unavailable):
    """The server did not accept a connection, now or recently."""


class DatasetUnavailable(Unavailable):
    """A dataset's read timed out or the server answered with a 5xx, now or recently."""


class BudgetExceeded(RuntimeError):
    """This instance has made as many requests to the server as its budget allows."""


class DatasetMissing(RuntimeError):
    """The server does not publish the dataset (a 404 that is not an empty window)."""


class FailureCache:
    """Hosts and datasets that failed recently, each with an expiry time and a reason. Thread safe.

    Keys are ``host:<netloc>`` and ``dataset:<netloc>/<dataset_id>``. ``ttl`` is given per entry: a number of
    seconds, ``None`` for an entry that never expires, or 0 (or less) to record nothing.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, str, str]] = {}
        self._lock = threading.Lock()

    def record(self, key: str, reason: str, ttl: float | None) -> None:
        if ttl is not None and ttl <= 0:
            return
        expires = math.inf if ttl is None else time.monotonic() + ttl
        since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self._entries[key] = (expires, reason, since)

    def check(self, key: str) -> str | None:
        """The recorded reason while ``key`` is still failed, else None."""
        entry = self._entry(key)
        return None if entry is None else entry[1]

    def since(self, key: str) -> str | None:
        """When ``key`` was recorded (UTC, ISO 8601) while it is still failed, else None."""
        entry = self._entry(key)
        return None if entry is None else entry[2]

    def _entry(self, key: str) -> tuple[float, str, str] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and time.monotonic() >= entry[0]:
                del self._entries[key]
                return None
            return entry

    def forget(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_DATASET_IN_PATH = re.compile(r"/(?:tabledap|griddap|info)/([^/.?]+)")
_UNITS_IN_HEADER = re.compile(r"^(.*?) \((.*)\)$")


def _stamp(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Erddap:
    """One ERDDAP server.

    ``budget`` caps the attempts this instance makes (None: no cap). ``retries`` is the number of attempts per
    request, ``backoff`` the first wait between them in seconds (doubled each time), and ``connect_timeout`` and
    ``timeout`` (the read timeout) are in seconds; ``get`` can override the last three per request.
    ``failure_ttl`` and ``failures`` set how failures are remembered (see the module docstring). ``fmt`` is ``"csv"``
    or ``"csvp"`` for ``fetch``. ``user_agent`` defaults to the one set with ``insitukit.identify``.

    One instance can serve several threads. A service that must fail fast uses ``retries=1``: a server that accepts
    and never answers then costs one read timeout, and a host that does not accept costs one connect timeout, shared
    by the reads in flight; later reads skip it until the entry expires.
    """

    def __init__(self, base_url: str, budget: int | None = 60, min_interval: float = 1.0,
                 connect_timeout: float = 10, user_agent: str | None = None, retries: int = 3, timeout: float = 90,
                 backoff: float = 5.0, failure_ttl: float | None = None, failures: FailureCache | None = None,
                 fmt: str = "csv"):
        if fmt not in ("csv", "csvp"):
            raise ValueError(f"fmt must be 'csv' or 'csvp', not {fmt!r}")
        if retries < 1:
            raise ValueError("retries counts attempts and must be at least 1")
        self.base_url = base_url.rstrip("/")
        self.host = urlsplit(self.base_url).netloc or self.base_url
        self.budget = budget
        self.min_interval = min_interval
        self.connect_timeout = connect_timeout
        self.retries = retries
        self.timeout = timeout
        self.backoff = backoff
        self.failure_ttl = failure_ttl
        self.failures = failures if failures is not None else FailureCache()
        self.fmt = fmt
        self.requests = 0
        self.bytes = 0
        self._lock = threading.Lock()
        self._last_start = 0.0
        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent or coops.USER_AGENT

    @property
    def unreachable(self) -> str | None:
        """When the host was recorded as unreachable (UTC, ISO 8601), while that entry lasts; else None."""
        return self.failures.since(self._host_key)

    @property
    def _host_key(self) -> str:
        return f"host:{self.host}"

    def _dataset_key(self, dataset_id: str) -> str:
        return f"dataset:{self.host}/{dataset_id}"

    def summary(self) -> str:
        budget = "no budget" if self.budget is None else f"a budget of {self.budget}"
        return f"{self.base_url}: {self.requests} requests of {budget}, {self.bytes / 1e6:.1f} MB"

    def get(self, url: str, retries: int | None = None, timeout: float | None = None,
            connect_timeout: float | None = None) -> requests.Response | None:
        """GET any URL on this server (an ``allDatasets`` listing, a ``.csv`` or ``.nc`` query) under the same
        pacing, budget, retries and failure cache as ``fetch``; None for an empty window."""
        return self._get(url, retries, timeout, connect_timeout)

    def _skip(self, url: str, dataset_id: str | None) -> None:
        reason = self.failures.check(self._host_key)
        if reason is not None:
            raise Unreachable(f"skipped after a recent failure: {reason}; not tried: {url}", url)
        if dataset_id is not None:
            reason = self.failures.check(self._dataset_key(dataset_id))
            if reason is not None:
                raise DatasetUnavailable(f"skipped after a recent failure: {reason}; not tried: {url}", url)

    def _start(self, url: str) -> None:
        """Count the attempt against the budget and wait for its turn. The slot is claimed under the lock and waited
        for outside it, so threads start ``min_interval`` apart without holding each other up."""
        with self._lock:
            if self.budget is not None and self.requests >= self.budget:
                raise BudgetExceeded(f"{self.requests} requests reached this run's budget; not sent: {url}")
            self.requests += 1
            now = time.monotonic()
            slot = max(now, self._last_start + self.min_interval)
            self._last_start = slot
        if slot > now:
            time.sleep(slot - now)

    def _get(self, url: str, retries: int | None = None, timeout: float | None = None,
             connect_timeout: float | None = None) -> requests.Response | None:
        retries = self.retries if retries is None else retries
        timeout = self.timeout if timeout is None else timeout
        connect_timeout = self.connect_timeout if connect_timeout is None else connect_timeout
        if retries < 1:
            raise ValueError("retries counts attempts and must be at least 1")
        match = _DATASET_IN_PATH.search(urlsplit(url).path)
        dataset_id = match.group(1) if match else None
        label = dataset_id or url
        self._skip(url, dataset_id)
        delay = self.backoff
        connect_failures = 0
        reason = ""
        dataset_failed = False
        attempt = 0
        for attempt in range(1, retries + 1):
            self._start(url)
            try:  # another thread may have recorded a failure while this one waited
                self._skip(url, dataset_id)
            except Unavailable:
                with self._lock:
                    self.requests -= 1  # not sent
                raise
            try:
                resp = self._session.get(url, timeout=(float(connect_timeout), float(timeout)))
            except requests.RequestException as exc:
                if isinstance(exc, requests.ConnectionError):  # includes ConnectTimeout, not ReadTimeout
                    connect_failures += 1
                    reason = f"{self.host} unreachable ({exc.__class__.__name__})"
                    dataset_failed = False
                elif isinstance(exc, requests.Timeout):
                    reason = f"{label} read timed out"
                    dataset_failed = True
                else:
                    reason = f"{label} request failed ({exc.__class__.__name__})"
                    dataset_failed = False
                log.warning("request failed (%s), attempt %d: %s", exc.__class__.__name__, attempt, url)
            else:
                content = getattr(resp, "content", None)
                with self._lock:
                    self.bytes += len(content) if content is not None else len(resp.text)
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 404 and "no matching results" in resp.text.lower():
                    return None
                log.warning("HTTP %d, attempt %d: %s", resp.status_code, attempt, url)
                if resp.status_code == 404:
                    raise DatasetMissing(f"dataset not found: {url}")
                reason = f"{label} returned HTTP {resp.status_code}"
                dataset_failed = resp.status_code >= 500
                if resp.status_code in (429, 503):
                    try:
                        retry_after = float((getattr(resp, "headers", None) or {}).get("Retry-After", ""))
                        delay = max(delay, min(60.0, retry_after))
                    except ValueError:
                        pass
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
        tries = f"{attempt} attempt" + ("s" if attempt != 1 else "")
        if connect_failures == attempt:
            self.failures.record(self._host_key, reason, self.failure_ttl)
            raise Unreachable(f"{reason}: no connection after {tries}: {url}", url)
        if dataset_failed:
            if self.failure_ttl is not None and dataset_id is not None:
                self.failures.record(self._dataset_key(dataset_id), reason, self.failure_ttl)
            raise DatasetUnavailable(f"{reason}; giving up after {tries}: {url}", url)
        raise Unavailable(f"{reason}; giving up after {tries}: {url}", url)

    def variables(self, dataset_id: str) -> list[str]:
        """The variable names a dataset publishes, from its ``info`` page."""
        resp = self._get(f"{self.base_url}/info/{dataset_id}/index.csv")
        if resp is None:
            raise DatasetMissing(f"no info for {dataset_id} on {self.base_url}")
        info = pd.read_csv(io.StringIO(resp.text))
        return info.loc[info["Row Type"] == "variable", "Variable Name"].tolist()

    def query_url(self, dataset_id: str, start: datetime, end: datetime, columns: Iterable[str],
                  depth: bool = False, depth_range: tuple[float | None, float | None] | None = None,
                  fmt: str | None = None) -> str:
        """The tabledap URL ``fetch`` asks for."""
        fmt = fmt or self.fmt
        names = ["time", *(["depth"] if depth or depth_range else []), *columns]
        url = (f"{self.base_url}/tabledap/{dataset_id}.{fmt}?{','.join(names)}"
               f"&time%3E={_stamp(start)}&time%3C{_stamp(end)}")
        if depth_range:
            low, high = depth_range
            if low is not None:
                url += f"&depth%3E={low:g}"
            if high is not None:
                url += f"&depth%3C={high:g}"
        return url

    def fetch(self, dataset_id: str, start: datetime, end: datetime, columns: list[str], depth: bool = False,
              depth_range: tuple[float | None, float | None] | None = None, fmt: str | None = None) -> pd.DataFrame:
        """Rows for ``start <= time < end``: a UTC ``time`` column and the requested columns (floats where numeric).

        Only the named columns are asked for. ``depth=True`` adds the ``depth`` column (many datasets have none);
        ``depth_range=(low, high)`` adds it too and keeps ``low <= depth <= high`` (either bound may be None).
        ``fmt`` overrides the instance's ``"csv"`` or ``"csvp"``; the frame is the same either way. The units the
        server reports are in ``df.attrs["units"]``.
        """
        fmt = fmt or self.fmt
        if fmt not in ("csv", "csvp"):
            raise ValueError(f"fmt must be 'csv' or 'csvp', not {fmt!r}")
        names = ["time", *(["depth"] if depth or depth_range else []), *columns]
        resp = self._get(self.query_url(dataset_id, start, end, columns, depth, depth_range, fmt))
        if resp is None:
            return pd.DataFrame({c: pd.Series(dtype="datetime64[ns, UTC]" if c == "time" else float) for c in names})
        text = resp.text
        if fmt == "csvp":  # one header row of "name (units)"
            df = pd.read_csv(io.StringIO(text))
            units = {}
            for header in df.columns:
                m = _UNITS_IN_HEADER.match(str(header))
                units[m.group(1) if m else str(header)] = m.group(2) if m else ""
            df.columns = list(units)
        else:  # row 2 of an ERDDAP CSV holds units
            head = pd.read_csv(io.StringIO("\n".join(text.splitlines()[:2])), keep_default_na=False, dtype=str)
            units = {c: (head[c].iloc[0] if len(head) else "") for c in head.columns}
            df = pd.read_csv(io.StringIO(text), skiprows=[1])
        df["time"] = pd.to_datetime(df["time"], utc=True)
        for c in names[1:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df.attrs["units"] = units
        return df

    def fetch_many(self, dataset_ids: Iterable[str], start: datetime, end: datetime, columns: list[str],
                   workers: int | None = None, **kwargs) -> tuple[dict[str, pd.DataFrame], dict[str, Exception]]:
        """``fetch`` several datasets at once on up to ``workers`` threads (default: one each), under this
        instance's pacing, budget and failure cache. Returns the frames and the failures, each keyed by dataset; a
        failure (``Unavailable``, ``DatasetMissing``, ``BudgetExceeded``) never loses the other datasets. ``kwargs``
        go to ``fetch``."""
        ids = list(dict.fromkeys(dataset_ids))
        frames: dict[str, pd.DataFrame] = {}
        failed: dict[str, Exception] = {}
        if not ids:
            return frames, failed

        def one(dataset_id: str) -> tuple[str, pd.DataFrame | None, Exception | None]:
            try:
                return dataset_id, self.fetch(dataset_id, start, end, columns, **kwargs), None
            except (Unavailable, DatasetMissing, BudgetExceeded) as exc:
                return dataset_id, None, exc

        with ThreadPoolExecutor(max_workers=workers or len(ids)) as pool:
            for dataset_id, df, exc in pool.map(one, ids):
                if df is None:
                    failed[dataset_id] = exc  # type: ignore[assignment]
                else:
                    frames[dataset_id] = df
        return frames, failed
