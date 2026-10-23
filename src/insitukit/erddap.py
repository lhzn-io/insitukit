"""A small ERDDAP tabledap client for servers other than the Sound's LISICOS (Merlin), first NERACOOS.

The request rules follow merlin.py, which stays as it is for the Sound: requests run one at a time, at least
``min_interval`` apart, each attempt counts against a per-run budget, a 503 or 429 with Retry-After is waited out,
and a 404 whose body says "no matching results" is an empty window, not a missing dataset. The state lives on the
instance, so a second server never shares the Sound's budget or its unreachable flag.
"""

from __future__ import annotations

import io
import logging
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from .merlin import USER_AGENT

log = logging.getLogger(__name__)

NERACOOS_URL = "https://data.neracoos.org/erddap"


class Unreachable(RuntimeError):
    """The server did not accept a connection on any retry earlier in this run."""


class BudgetExceeded(RuntimeError):
    """This run has made as many requests to the server as its budget allows."""


class Erddap:
    def __init__(self, base_url: str, budget: int = 60, min_interval: float = 1.0, connect_timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.budget = budget
        self.min_interval = min_interval
        self.connect_timeout = connect_timeout
        self.requests = 0
        self.bytes = 0
        self.unreachable: str | None = None
        self._last_start = 0.0
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT

    def summary(self) -> str:
        return f"{self.base_url}: {self.requests} requests of a budget of {self.budget}, {self.bytes / 1e6:.1f} MB"

    def _get(self, url: str, retries: int = 3, timeout: int = 90) -> requests.Response | None:
        if self.unreachable:
            raise Unreachable(f"{self.base_url} unreachable since {self.unreachable}; not tried: {url}")
        delay = 5.0
        connect_failures = 0
        for attempt in range(1, retries + 1):
            if self.requests >= self.budget:
                raise BudgetExceeded(f"{self.requests} requests reached this run's budget; not sent: {url}")
            wait = self._last_start + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_start = time.monotonic()
            self.requests += 1
            try:
                resp = self._session.get(url, timeout=(self.connect_timeout, timeout))
            except requests.RequestException as exc:
                if isinstance(exc, (requests.ConnectTimeout, requests.ConnectionError)) and not isinstance(exc, requests.ReadTimeout):
                    connect_failures += 1
                log.warning("request failed (%s), attempt %d: %s", exc.__class__.__name__, attempt, url)
            else:
                self.bytes += len(resp.content)
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 404 and "no matching results" in resp.text.lower():
                    return None
                log.warning("HTTP %d, attempt %d: %s", resp.status_code, attempt, url)
                if resp.status_code == 404:
                    raise RuntimeError(f"dataset not found: {url}")
                if resp.status_code in (429, 503):
                    try:
                        delay = max(delay, min(60.0, float(resp.headers.get("Retry-After", ""))))
                    except ValueError:
                        pass
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
        if connect_failures == retries:
            self.unreachable = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            raise Unreachable(f"no connection after {retries} attempts: {url}")
        raise RuntimeError(f"giving up after {retries} attempts: {url}")

    def fetch(self, dataset_id: str, start: datetime, end: datetime, columns: list[str]) -> pd.DataFrame:
        """Rows for ``start <= time < end``: a UTC ``time`` column and the requested columns (floats where numeric).

        Unlike merlin.fetch, no ``depth`` column is asked for: many NERACOOS datasets have none.
        """
        stamp = lambda ts: ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        query = ",".join(["time", *columns])
        url = f"{self.base_url}/tabledap/{dataset_id}.csv?{query}&time%3E={stamp(start)}&time%3C{stamp(end)}"
        resp = self._get(url)
        if resp is None:
            return pd.DataFrame({c: pd.Series(dtype="datetime64[ns, UTC]" if c == "time" else float) for c in ["time", *columns]})
        df = pd.read_csv(io.StringIO(resp.text), skiprows=[1])  # row 2 of an ERDDAP CSV holds units
        df["time"] = pd.to_datetime(df["time"], utc=True)
        for c in columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df
