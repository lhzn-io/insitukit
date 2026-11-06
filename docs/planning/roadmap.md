# insitukit roadmap

Planning notes for the library: where it stands, who uses it, and what it lacks before other code can rely on it.
Updated 2026-11-05.

## Status

insitukit 0.1.0 (PyPI, 2026-11-02) came out of the lhzn-blue ingest jobs, with their history. It covers:

- `coops`: CO-OPS observations (windows split to the API's limits), 6-minute tide predictions, high and low tide
  events, and station metadata. Requests are paced 1 s apart, retried with backoff, and 403 and 429 are treated as
  throttling.
- `erddap`: a tabledap client for any server, with a per-run request budget and an unreachable flag. 0.3.0 makes
  it fit for long-running services (see below).
- `stats`, `series` and `store`: record statistics, the compact JSON time-series form, and a guarded store.

## Adoption

A survey of the organisation's repositories on 2026-11-05 found CO-OPS or ERDDAP code duplicated in several places.
The table ranks the candidates; internal projects are described rather than named.

| Consumer | Duplicated code | Needs | Order |
| :--- | :--- | :--- | :--- |
| lhzn-blue ingest jobs | CO-OPS and ERDDAP clients (moved to insitukit, awaiting merge); tide predictions, datums and flood levels; harmonic constants; a second ERDDAP client for the UConn server | 0.2.0 for the remainder | 1, then 4 |
| forcingkit | CO-OPS water level (no window split, pacing or retries), current predictions and current stations (moved in forcingkit 0.14.0); a UConn ERDDAP reader | 0.2.0 for CO-OPS; 0.3.0 for ERDDAP | 3 |
| An internal model-validation suite and its analysis scripts | CO-OPS water level, predictions, wind, currents by bin, current predictions, station lists | 0.2.0 | 5 |
| Course material scripts | CO-OPS currents by bin, current predictions, station bins and deployments | 0.2.0 | 6 |
| A vessel logger | CO-OPS water level and predictions on a NAVD88 datum | 0.2.0; Python 3.11 on the device | 7 |
| A demo app | ERDDAP downloads for latest readings | 0.1.0 | 7 |

## 0.2.0: what adoption needs now

CO-OPS:

- Thread-safe pacing, so a server answering requests on several threads keeps the 1 s spacing.
- The error message of a 4xx answer is read and raised; a missing datum raises `DatumUnavailable`, so a caller can
  fall back (for example from NAVD88 to MSL).
- `fetch("currents", ..., bin=)`: speed (m/s; the API reports cm/s even in metric units), direction and bin.
- `current_predictions(station, bin, start, end, interval)`: speed along the flood axis in m/s, with the flood and
  ebb directions and bin depth; `MAX_SLACK` events with their type. Subordinate stations are refused.
- `predictions(..., interval="h")` and a `units` choice, so callers that publish feet need not convert.
- Station metadata: `datums`, `flood_levels`, `harcon` (by bin for current stations), `stations(type=, bbox=)`, and
  `metadata(expand=)` for deployments and bins.

ERDDAP (what a long-standing client of a single server needs):

- `variables(dataset_id)`, a public `get_csv(url)`, `budget=None` for no limit, `retries` and `timeout` settings,
  and `DatasetMissing` for a dataset the server does not have.

## 0.3.0: ERDDAP for long-running services

What forcingkit's UConn reader needs before it can move, without changing 0.2 behaviour for batch callers:

- An expiring failure cache (`FailureCache`, shareable between instances). `failure_ttl=None`, the default, is the
  0.2 behaviour: a host that never accepted a connection is skipped for the life of the instance, and datasets are
  not recorded. A number of seconds records both the host and any dataset whose read timed out or answered with a
  5xx, and skips them until the entry expires; 0 records nothing. `unreachable` is now read from the cache.
- Thread safety: one instance serves several threads; pacing slots, the budget and byte counts are under a lock.
  `fetch_many` reads several datasets on parallel threads and returns `(frames, failed)`, so one failure never
  loses the others.
- `fmt="csvp"` (per instance or per `fetch`), units in `df.attrs["units"]` for both formats, and `depth=True` or
  `depth_range=(low, high)`.
- Control over `connect_timeout`, `timeout` (read), `retries` (attempts) and `backoff`, per instance; `get` takes the
  timeouts and retries per request.
- A fast failure path: with `retries=1`, a server that accepts and never answers costs one read timeout, and an
  unreachable host costs one connect timeout shared by the reads in flight; later reads skip it at once.
- New exceptions: `Unavailable` (base, a `RuntimeError`), `Unreachable` (now a subclass) and `DatasetUnavailable`.
  Retries exhausted still say "giving up after N attempts", so callers that match on that phrase keep working.

Offline tests cover each point, including a real socket that never answers (under 2 s for three datasets).

### Moving forcingkit's UConn reader onto 0.3.0

Checked against a scratch copy of forcingkit 0.14.0: all 19 tests in `tests/unit/test_erddap.py` and the other 386
unit tests pass with the same assertions; only what the tests patch changes.

- `pyproject.toml`: `insitukit>=0.3,<0.4`.
- `src/forcingkit/fetchers/erddap.py`: delete `ErddapUnavailable`, `_FailureCache`, `_read_dataset` and the
  `io`, `threading`, `time`, `ThreadPoolExecutor`, `urlsplit` and `requests` imports. Keep `BASE_ERDDAP_URL` and
  `clear_failure_cache()`, which now clears a module-level `insitukit.erddap.FailureCache`. Add `_server()`, built
  per call so the settings are read each time: `Erddap(BASE_ERDDAP_URL, budget=None, min_interval=0.2, retries=1,
  connect_timeout=connect, timeout=read, failure_ttl=settings.erddap_failure_ttl(), failures=_failures,
  fmt="csvp")`, with `(connect, read) = settings.erddap_timeouts()`.
- `fetch_erddap_station_profiles`: keep the per-dataset CSV cache; read the datasets not in it with
  `_server().fetch_many(names, start, end, ["sea_water_temperature"])`, set the index to `time` before writing the
  cache, and append a skipped entry (`reason=str(exc)`) for each failure. Drop `DatasetMissing` from the failures to
  keep today's output, where any 404 is a dataset with no rows. The function's signature and return shape do not
  change, nor does `fetch_erddap_stations_in_bbox`.
- `tests/unit/test_erddap.py`: patch `requests.Session.get` instead of `forcingkit.fetchers.erddap.requests.get`
  (the fake gains a `self` argument), and `time.monotonic` instead of `erddap.time.monotonic`. Nothing else changes.
- Reasons in `skipped` keep their phrases ("unreachable", "read timed out", "returned HTTP 503", "skipped after a
  recent failure") and now end with "giving up after 1 attempt", "no connection after 1 attempt" or "not tried",
  and the URL.
- Requests now send the User-Agent given to `insitukit.identify`, and start at least 0.2 s apart.

## 0.4.0: NDBC

- `insitukit.ndbc` is ported from forcingkit's NDBC fetcher.
  - It downloads each file once per call; the old fetcher fetched a yearly file again for every month of a window.
  - It reads the column names of files from before 2007 (`WD`, `BAR`).
  - It applies each column's own sentinel. The fetcher treated 99, 999 and 9999 as missing in every column, which
    dropped real readings such as a 999.0 hPa low or a 99-degree wind.
- forcingkit's `fetchers/ndbc.py` becomes a thin layer over it. Its live output is identical except for those
  readings, now kept, and a source list without repeats.
- `coops.current_predictions` accepts a window whose single event CO-OPS returns as an object, not a one-item list.

## Later
- `series` parity with lhzn-blue's own encoder, which keeps leading and trailing gaps, always uses an hourly grid,
  converts temperatures to degrees F and skips marker lists. Either `series` gains those options or the consumer
  keeps a thin wrapper.
- Consumers' own exceedance, joint histogram and climatology code, to be compared with `stats` and folded in.
- An optional on-disk response cache (several consumers keep their own).
- Quality flags (`f`, `q`) on water level, for verified against preliminary data.
- A lookup of the nearest station of a type, so consumers can replace hand-kept station lists with a query.

## Out of scope

Site-, partner- or project-specific station registries and defaults stay in the projects that use the library
(see `AGENTS.md`).
