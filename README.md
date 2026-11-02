# insitukit

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

Fetch, summarise and publish in situ ocean observations: NOAA CO-OPS and ERDDAP clients, record statistics, and a
compact JSON time-series format.

forcingkit prepares what drives a coastal model and topobathykit builds the ground it runs on; insitukit handles what
the instruments in the water actually measured. It reads tide gauges and buoys from NOAA CO-OPS and any ERDDAP server
with care for the provider: requests are paced, budgeted and identified, and an empty window is an empty result, not
an error. It reduces long records to the summaries people read: daily means only where a day is at least half
covered, exceedance curves, joint histograms, and marine heatwave climatologies after Hobday et al. (2016). And it
writes them as small JSON series behind a guard that refuses unexpected object names or sizes, so a web page can draw
years of data from a few files.

## Install

```bash
pip install insitukit            # CO-OPS, ERDDAP, statistics, the JSON form, local and Workers KV storage
pip install "insitukit[gcs]"     # also Google Cloud Storage
```

## Use

```python
from datetime import datetime, timezone

import insitukit
from insitukit import coops, series, stats
from insitukit.erddap import Erddap

insitukit.identify("my-site/1.0 (+https://example.org; data@example.org)", application="my-site")

start, end = datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc)

# Observed water level and the station's own predictions; their difference is the surge.
level = coops.fetch("water_level", "8419870", start, end).set_index("time")["water_level"]
surge = level - coops.predictions("8419870", start, end).reindex(level.index)

# A buoy's temperature from an ERDDAP server, at most 20 requests this run.
server = Erddap("https://data.neracoos.org/erddap", budget=20)
temp = server.fetch("UNH_CML", start, end, ["temperature"]).set_index("time")["temperature"]

daily = stats.day_means(temp, per_day=24, digits=2, tz="America/New_York")
hourly = series.encode(surge.resample("h").mean(), unit="m", places=3)  # {"t0": ..., "step": 3600, ...}
```

## Modules

| Module | Contents |
| :--- | :--- |
| `insitukit.coops` | `fetch` (observations; long windows split to the API's limits), `predictions` (6-minute tides), `tide_events` (highs and lows), `metadata`. Metric units and UTC throughout. |
| `insitukit.erddap` | `Erddap(base_url, budget, min_interval)`: tabledap CSV into a DataFrame, with a per-run request budget and an unreachable flag. |
| `insitukit.stats` | `day_means` (coverage rule), `exceedance`, `joint` (two series paired by nearest time), `climatology` (Hobday et al. 2016: 11-day window, 31-day smoothing, mean, SD, 90th percentile). |
| `insitukit.series` | `encode` and `decode` for one series, `encode_frame` and `decode_frame` for several on one grid, `iso`. |
| `insitukit.store` | `Store(root, writable, max_bytes)`: JSON and bytes to a directory, `kv://<account>/<namespace>` (Cloudflare Workers KV, token in `CLOUDFLARE_API_TOKEN`) or `gs://bucket/prefix`, refusing names outside `writable` and objects over `max_bytes`. |

## Etiquette

CO-OPS requests are at least 1 s apart, and 403 and 429 answers are treated as throttling and retried with backoff.
ERDDAP requests are at least `min_interval` apart and stop at the run's `budget`. Both send the User-Agent given to
`identify`; include a way for the provider to reach you.

## Reference

Hobday, A. J., et al. (2016). A hierarchical approach to defining marine heatwaves. *Progress in Oceanography*, 141,
227-238. doi:10.1016/j.pocean.2015.12.014

## License

MIT. Copyright (c) 2026 Long Horizon Observatory.
