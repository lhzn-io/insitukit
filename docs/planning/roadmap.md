# insitukit roadmap

Planning notes for the library: where it stands, who uses it, and what it lacks before other code can rely on it.
Updated 2026-11-05.

## Status

insitukit 0.1.0 (PyPI, 2026-11-02) came out of the lhzn-blue ingest jobs, with their history. It covers:

- `coops`: CO-OPS observations (windows split to the API's limits), 6-minute tide predictions, high and low tide
  events, and station metadata. Requests are paced 1 s apart, retried with backoff, and 403 and 429 are treated as
  throttling.
- `erddap`: a tabledap client for any server, with a per-run request budget and an unreachable flag.
- `stats`, `series` and `store`: record statistics, the compact JSON time-series form, and a guarded store.

## Adoption

A survey of the organisation's repositories on 2026-11-05 found CO-OPS or ERDDAP code duplicated in several places.
The table ranks the candidates; internal projects are described rather than named.

| Consumer | Duplicated code | Needs | Order |
| :--- | :--- | :--- | :--- |
| lhzn-blue ingest jobs | CO-OPS and ERDDAP clients (moved to insitukit, awaiting merge); tide predictions, datums and flood levels; harmonic constants; a second ERDDAP client for the UConn server | 0.2.0 for the remainder | 1, then 4 |
| forcingkit | CO-OPS water level (no window split, pacing or retries), current predictions and current stations; a UConn ERDDAP reader | 0.2.0 for CO-OPS; 0.3.0 for ERDDAP | 3 |
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

## 0.3.0 and later

- ERDDAP for long-running services: a failure cache that expires (a host or dataset is skipped for a few minutes,
  not for the rest of the process), thread safety, parallel reads of several datasets, `.csvp`, depth, and a fast
  failure path for servers that do not answer. This is what forcingkit's UConn reader needs before it can move.
- NDBC: standard meteorological and ADCP records are in situ observations. Decide whether forcingkit's NDBC fetcher
  moves here, with forcingkit re-exporting it.
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
