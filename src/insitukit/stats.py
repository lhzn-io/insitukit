"""Summaries of a long record: daily means with a coverage rule, exceedance, a joint histogram of two series measured
at nearly the same times, and a marine heatwave climatology as Hobday et al. (2016) build theirs
(doi:10.1016/j.pocean.2015.12.014).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def day_means(s: pd.Series, per_day: int, digits: int, how: str = "mean", tz: str = "UTC") -> pd.Series:
    """Daily means (or another resampler such as ``"max"``) in ``tz`` of a series indexed by aware times, kept only for
    a day with at least half its ``per_day`` records."""
    s = s.dropna()
    s.index = s.index.tz_convert(tz)
    g = s.resample("D")
    out = getattr(g, how)().where(g.count() >= per_day / 2)
    return out.round(digits)


def exceedance(values, xs: list[float]) -> list[float | None]:
    """The share of the finite values above each x (to 4 decimals); None for each x when there are none."""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    return [round(float((v > x).mean()), 4) if v.size else None for x in xs]


def joint(a: pd.Series, b: pd.Series, a_edges: list[float], b_edges: list[float], pair_s: int) -> list[list[int]]:
    """Counts of ``a`` by its bins (rows) against ``b`` at the nearest time within ``pair_s`` seconds (columns).
    Bins are ``[edge, next edge)``; the last is open, and a value below the first edge counts in the first."""
    counts = np.zeros((len(a_edges), len(b_edges)), dtype=int)
    if a.empty or b.empty:
        return counts.tolist()
    left = pd.DataFrame({"a": a}).sort_index()
    right = pd.DataFrame({"b": b}).sort_index()
    m = pd.merge_asof(left, right, left_index=True, right_index=True, direction="nearest",
                      tolerance=pd.Timedelta(seconds=pair_s)).dropna()
    i = np.clip(np.searchsorted(a_edges, m["a"].to_numpy(), side="right") - 1, 0, len(a_edges) - 1)
    j = np.clip(np.searchsorted(b_edges, m["b"].to_numpy(), side="right") - 1, 0, len(b_edges) - 1)
    np.add.at(counts, (i, j), 1)
    return counts.tolist()


def climatology(days: pd.Series, window: int = 5, smooth: int = 31) -> dict:
    """A day-of-year climatology from daily means indexed by date: for each day, every daily mean within ``window``
    days of it across the years, then the mean, standard deviation and 90th percentile, each smoothed with a
    ``smooth``-day running mean (Hobday et al. 2016 use 5 and 31).

    Returns 366 values of each (``mean``, ``sd``, ``p90``, ``n``): day 60 (29 February) repeats 28 February's, so a
    reader can index by day of year in any year.
    """
    d = days.dropna()
    # Days 1 to 365: in a leap year 29 February joins 28 February and every later day moves back one.
    idx = pd.DatetimeIndex(d.index)
    day = np.asarray(idx.day_of_year)
    doy = day - (np.asarray(idx.is_leap_year) & (day >= 60))
    vals = d.to_numpy()
    mean, sd, p90, n = [], [], [], []
    for k in range(1, 366):
        dist = np.minimum(np.abs(doy - k), 365 - np.abs(doy - k))
        w = vals[dist <= window]
        n.append(int(w.size))
        mean.append(float(w.mean()) if w.size else math.nan)
        sd.append(float(w.std(ddof=1)) if w.size > 1 else math.nan)
        p90.append(float(np.percentile(w, 90)) if w.size else math.nan)

    def smoothed(x: list[float]) -> list[float | None]:
        s = pd.Series(x * 3).rolling(smooth, center=True, min_periods=smooth // 2).mean().iloc[365:730]
        return [None if pd.isna(v) else round(float(v), 2) for v in s]

    def leap(x: list) -> list:
        return x[:59] + [x[58]] + x[59:]

    return {"mean": leap(smoothed(mean)), "sd": leap(smoothed(sd)), "p90": leap(smoothed(p90)), "n": leap(n)}
