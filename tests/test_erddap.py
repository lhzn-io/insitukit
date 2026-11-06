"""The ERDDAP client for long-running services: the expiring failure cache, threads, parallel reads, ``.csvp``,
depth and the fast failure path. Offline: HTTP calls are replaced, except for one local socket that never answers."""

import socket
import threading
import time
from datetime import datetime, timezone

import pytest
import requests

from insitukit import erddap

URL = "https://erddap.example.org/erddap"
START = datetime(2024, 5, 1, tzinfo=timezone.utc)
END = datetime(2024, 5, 1, 6, tzinfo=timezone.utc)
CSV = "time,sea_water_temperature\nUTC,degree_C\n2024-05-01T00:00:00Z,10.5\n2024-05-01T01:00:00Z,NaN\n"
CSVP = "time (UTC),sea_water_temperature (degree_C)\n2024-05-01T00:00:00Z,10.5\n2024-05-01T01:00:00Z,NaN\n"
LEVELS = ["WLIS_WQ_SFC", "WLIS_WQ_MID", "WLIS_WQ_BTM"]


class FakeResponse:
    """Only what a minimal fake offers: a status and a body."""

    def __init__(self, status_code=200, text=CSV):
        self.status_code = status_code
        self.text = text


def install(monkeypatch, server, behaviour):
    """Replace the server's HTTP GET; ``behaviour(url, timeout)`` returns a response or raises."""
    calls = []
    lock = threading.Lock()

    def get(url, timeout=None):
        with lock:
            calls.append((url, timeout))
        return behaviour(url, timeout)

    monkeypatch.setattr(server._session, "get", get)
    return calls


def raises(exc):
    def behaviour(url, timeout):
        raise exc

    return behaviour


def service(**kw):
    """The settings a long-running service uses: one attempt, a five-minute failure cache, light pacing."""
    return erddap.Erddap(URL, **{"budget": None, "min_interval": 0.0, "retries": 1, "failure_ttl": 300, **kw})


def test_csv_and_csvp_give_the_same_frame_with_units(monkeypatch):
    server = service()
    install(monkeypatch, server, lambda url, timeout: FakeResponse(text=CSVP if ".csvp?" in url else CSV))
    a = server.fetch("WLIS_WQ_SFC", START, END, ["sea_water_temperature"])
    b = server.fetch("WLIS_WQ_SFC", START, END, ["sea_water_temperature"], fmt="csvp")
    for df in (a, b):
        assert list(df.columns) == ["time", "sea_water_temperature"]
        assert str(df["time"].dt.tz) == "UTC" and df["sea_water_temperature"].iloc[0] == 10.5
        assert df.attrs["units"] == {"time": "UTC", "sea_water_temperature": "degree_C"}
    assert erddap.Erddap(URL, fmt="csvp").query_url("ds", START, END, ["t"]).startswith(f"{URL}/tabledap/ds.csvp?")
    with pytest.raises(ValueError):
        erddap.Erddap(URL, fmt="json")


def test_depth_is_asked_for_and_bounded(monkeypatch):
    server = service()
    url = server.query_url("ds", START, END, ["t"], depth_range=(0, 5.5))
    assert "?time,depth,t&" in url and url.endswith("&depth%3E=0&depth%3C=5.5")
    assert "depth" not in server.query_url("ds", START, END, ["t"])
    assert "?time,depth,t&" in server.query_url("ds", START, END, ["t"], depth=True)
    body = "time,depth,t\nUTC,m,degree_C\n2024-05-01T00:00:00Z,2.0,10.5\n"
    install(monkeypatch, server, lambda url, timeout: FakeResponse(text=body))
    df = server.fetch("ds", START, END, ["t"], depth=True)
    assert list(df.columns) == ["time", "depth", "t"] and df["depth"].iloc[0] == 2.0


def test_timeouts_are_passed_as_connect_and_read(monkeypatch):
    server = service(connect_timeout=2, timeout=7)
    calls = install(monkeypatch, server, lambda url, timeout: FakeResponse())
    server.fetch("ds", START, END, ["sea_water_temperature"])
    server.get(f"{URL}/tabledap/ds.csv?time", timeout=3, connect_timeout=1)
    assert [t for _, t in calls] == [(2.0, 7.0), (1.0, 3.0)]


def test_an_unreachable_host_costs_one_connect_timeout(monkeypatch):
    """Three datasets read in parallel wait out one connect timeout together; the next read skips the host."""
    server = service(connect_timeout=0.5)

    def unreachable(url, timeout):
        time.sleep(timeout[0])
        raise requests.ConnectTimeout("connect timed out")

    calls = install(monkeypatch, server, unreachable)
    t0 = time.monotonic()
    frames, failed = server.fetch_many(LEVELS, START, END, ["sea_water_temperature"])
    assert time.monotonic() - t0 < 0.9
    assert frames == {} and set(failed) == set(LEVELS)
    assert all(isinstance(e, erddap.Unreachable) and "unreachable" in str(e) for e in failed.values())
    first = len(calls)
    t0 = time.monotonic()
    _, failed = server.fetch_many(["EXRX_WQ_SFC", "EXRX_WQ_MID"], START, END, ["sea_water_temperature"])
    assert time.monotonic() - t0 < 0.1 and len(calls) == first
    assert all("recent failure" in str(e) and "unreachable" in str(e) for e in failed.values())
    assert server.unreachable is not None


def test_a_silent_server_fails_within_the_read_timeout():
    """A real socket that accepts and never answers."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    accepted, stop = [], threading.Event()

    def accept_forever():
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                accepted.append(listener.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=accept_forever, daemon=True)
    thread.start()
    try:
        server = erddap.Erddap(f"http://127.0.0.1:{listener.getsockname()[1]}/erddap", budget=None,
                               min_interval=0.2, retries=1, timeout=0.5, failure_ttl=300)
        t0 = time.monotonic()
        frames, failed = server.fetch_many(LEVELS, START, END, ["sea_water_temperature"], fmt="csvp")
        elapsed = time.monotonic() - t0
    finally:
        stop.set()
        thread.join()
        for conn in accepted:
            conn.close()
        listener.close()
    assert elapsed < 2.0, elapsed
    assert frames == {} and set(failed) == set(LEVELS)
    assert all(isinstance(e, erddap.DatasetUnavailable) and "read timed out" in str(e) for e in failed.values())


def test_a_failed_dataset_is_skipped_until_its_entry_expires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(erddap.time, "monotonic", lambda: clock[0])
    server = service(failure_ttl=60)
    calls = install(monkeypatch, server, lambda url, timeout: FakeResponse(503, ""))
    server.fetch_many(LEVELS, START, END, ["t"])
    _, failed = server.fetch_many(LEVELS, START, END, ["t"])
    assert len(calls) == 3  # each dataset's 503 is remembered
    assert all("recent failure" in str(e) and "HTTP 503" in str(e) for e in failed.values())
    clock[0] += 61
    server.fetch_many(LEVELS, START, END, ["t"])
    assert len(calls) == 6


def test_a_ttl_of_zero_remembers_nothing(monkeypatch):
    server = service(failure_ttl=0)
    calls = install(monkeypatch, server, raises(requests.ConnectionError("refused")))
    server.fetch_many(LEVELS, START, END, ["t"])
    server.fetch_many(LEVELS, START, END, ["t"])
    assert len(calls) == 6 and server.unreachable is None


def test_no_rows_is_not_a_failure(monkeypatch):
    server = service()
    calls = install(monkeypatch, server, lambda url, timeout: FakeResponse(404, "Your query produced no matching results."))
    for _ in range(2):
        frames, failed = server.fetch_many(LEVELS, START, END, ["t"])
        assert failed == {} and all(df.empty for df in frames.values())
    assert len(calls) == 6


def test_a_shared_cache_spans_instances_and_hosts_stay_apart(monkeypatch):
    shared = erddap.FailureCache()
    a = service(failures=shared)
    install(monkeypatch, a, raises(requests.ConnectionError("refused")))
    with pytest.raises(erddap.Unreachable):
        a.fetch("ds", START, END, ["sea_water_temperature"])
    b = service(failures=shared)
    calls = install(monkeypatch, b, lambda url, timeout: FakeResponse())
    with pytest.raises(erddap.Unreachable, match="recent failure"):
        b.fetch("ds", START, END, ["sea_water_temperature"])
    other = erddap.Erddap("https://other.example.org/erddap", budget=None, min_interval=0, retries=1, failures=shared)
    install(monkeypatch, other, lambda url, timeout: FakeResponse())
    assert not other.fetch("ds", START, END, ["sea_water_temperature"]).empty
    assert calls == []
    shared.clear()
    assert not b.fetch("ds", START, END, ["sea_water_temperature"]).empty


def test_batch_mode_keeps_the_host_for_the_run_and_does_not_record_datasets(monkeypatch):
    """The 0.2 behaviour: no failure_ttl, three attempts, the host remembered for the life of the instance."""
    clock = [1000.0]
    monkeypatch.setattr(erddap.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(erddap.time, "sleep", lambda s: None)
    server = erddap.Erddap(URL, budget=None, min_interval=0)
    calls = install(monkeypatch, server, lambda url, timeout: FakeResponse(503, ""))
    for _ in range(2):
        with pytest.raises(erddap.DatasetUnavailable, match="giving up after 3 attempts"):
            server.fetch("ds", START, END, ["t"])
    assert len(calls) == 6  # a 5xx dataset is not remembered in batch mode
    calls = install(monkeypatch, server, raises(requests.ConnectionError("refused")))
    with pytest.raises(erddap.Unreachable, match="no connection after 3 attempts"):
        server.fetch("ds", START, END, ["t"])
    clock[0] += 10 * 86400
    with pytest.raises(erddap.Unreachable, match="recent failure"):
        server.fetch("other", START, END, ["t"])
    assert len(calls) == 3 and server.unreachable
    assert issubclass(erddap.Unreachable, RuntimeError) and issubclass(erddap.DatasetUnavailable, RuntimeError)


def test_threads_share_the_pacing_and_the_budget(monkeypatch):
    server = erddap.Erddap(URL, budget=4, min_interval=0.05, retries=1)
    stamps = []
    lock = threading.Lock()

    def get(url, timeout):
        with lock:
            stamps.append(time.monotonic())
        return FakeResponse()

    install(monkeypatch, server, get)
    frames, failed = server.fetch_many([f"ds{i}" for i in range(6)], START, END, ["sea_water_temperature"])
    assert len(frames) == 4 and len(failed) == 2
    assert all(isinstance(e, erddap.BudgetExceeded) for e in failed.values())
    assert server.requests == 4
    stamps.sort()
    assert all(b - a >= 0.045 for a, b in zip(stamps, stamps[1:]))


def test_other_failures_are_unavailable_but_not_remembered(monkeypatch):
    server = service()
    calls = install(monkeypatch, server, lambda url, timeout: FakeResponse(400, "Query error"))
    for _ in range(2):
        with pytest.raises(erddap.Unavailable, match="HTTP 400; giving up after 1 attempt:"):
            server.fetch("ds", START, END, ["t"])
    assert len(calls) == 2
    install(monkeypatch, server, lambda url, timeout: FakeResponse(404, "Resource not found"))
    _, failed = server.fetch_many(["nope"], START, END, ["t"])
    assert isinstance(failed["nope"], erddap.DatasetMissing)
