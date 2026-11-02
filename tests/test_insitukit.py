"""Statistics, the JSON series form, the store's guard, the CO-OPS and ERDDAP clients and identification. Offline:
the clients' HTTP calls are replaced."""

import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import insitukit
from insitukit import coops, erddap, series, stats
from insitukit.store import Store


def test_a_day_with_less_than_half_its_records_has_no_mean():
    t = pd.date_range("2026-07-01", periods=96 * 2, freq="15min", tz="UTC")
    s = pd.Series(10.0, index=t)
    s.iloc[96 + 50:] = np.nan  # the second day keeps 50 of 96
    s.iloc[96:96 + 47] = np.nan  # and then only 3
    out = stats.day_means(s, 96, 2)
    assert out.iloc[0] == 10.0 and pd.isna(out.iloc[1])


def test_exceedance_counts_the_share_strictly_above():
    assert stats.exceedance([0.1, 0.2, 0.3, np.nan], [0.0, 0.2, 0.3]) == [1.0, 0.3333, 0.0]
    assert stats.exceedance([], [1.0]) == [None]


def test_joint_pairs_the_nearest_time_within_the_tolerance():
    t = pd.date_range("2026-07-01", periods=3, freq="20min", tz="UTC")
    a = pd.Series([0.05, 0.25, 4.0], index=t)
    b = pd.Series([5.0, 35.0], index=[t[0] + pd.Timedelta(minutes=2), t[2] + pd.Timedelta(minutes=20)])
    counts = stats.joint(a, b, [0.0, 0.2, 3.0], [0, 20], pair_s=600)
    assert counts == [[1, 0], [0, 0], [0, 0]]  # only the first burst has a partner within 10 minutes


def test_climatology_of_a_seasonal_cycle_follows_it():
    t = pd.date_range("2008-01-01", "2017-12-31", freq="D")
    days = pd.Series(10 + 8 * np.sin(2 * np.pi * (t.day_of_year - 110) / 365.25), index=t)
    c = stats.climatology(days)
    assert len(c["mean"]) == 366 and c["mean"][59] == c["mean"][58]
    assert c["mean"][200] == pytest.approx(10 + 8 * np.sin(2 * np.pi * 90 / 365.25), abs=0.3)
    assert all(p >= m for p, m in zip(c["p90"], c["mean"]))
    assert min(c["n"]) >= 10 * 11 - 3


def test_the_store_refuses_names_outside_its_pattern_and_oversized_objects(tmp_path):
    store = Store(str(tmp_path), re.compile(r"v1/a\.json"), max_bytes=10)
    store.write_json("v1/a.json", {"x": 1}, max_age=60)
    assert store.read_json("v1/a.json") == {"x": 1}
    with pytest.raises(ValueError, match="unexpected object name"):
        store.write_json("v1/b.json", {}, max_age=60)
    with pytest.raises(ValueError, match="exceeds"):
        store.write_bytes("v1/a.json", b"x" * 11, max_age=60)


def test_predictions_read_every_window_and_keep_the_range(monkeypatch):
    calls = []

    def fake_get(params):
        calls.append(params)
        lo = datetime.strptime(params["begin_date"], "%Y%m%d %H:%M")
        return {"predictions": [{"t": (lo + pd.Timedelta(minutes=6 * k)).strftime("%Y-%m-%d %H:%M"), "v": str(k)} for k in range(3)]}

    monkeypatch.setattr(coops, "_get", fake_get)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    s = coops.predictions("8419870", start, datetime(2026, 2, 5, tzinfo=timezone.utc))
    assert len(calls) == 2 and calls[0]["interval"] == "6" and calls[0]["datum"] == "MLLW"
    assert len(s) == 6 and s.index[0] == pd.Timestamp(start) and s.iloc[1] == 1.0


def test_a_series_round_trips_with_its_gaps():
    t = pd.date_range("2026-10-01", periods=5, freq="h", tz="UTC")
    s = pd.Series([0.41234, 0.44, np.nan, 0.52, np.nan], index=t)
    obj = series.encode(s, "m", 2)
    assert obj == {"t0": "2026-10-01T00:00:00Z", "step": 3600, "unit": "m", "values": [0.41, 0.44, None, 0.52]}
    back = series.decode(obj)
    assert list(back.index) == [t[0], t[1], t[3]] and back.iloc[2] == 0.52


def test_a_frame_round_trips_and_skips_marked_lists():
    t = pd.date_range("2026-10-01", periods=3, freq="30min", tz="UTC")
    f = pd.DataFrame({"wind_kt": [8.14, np.nan, 9.0], "dir_deg": [215.0, np.nan, 220.0]}, index=t)
    obj = series.encode_frame(f, {"wind_kt": 1, "dir_deg": 0}, step=1800)
    assert obj["step"] == 1800 and obj["wind_kt"] == [8.1, None, 9.0] and obj["dir_deg"] == [215.0, None, 220.0]
    obj["flagged"] = [1]
    back = series.decode_frame(obj, skip=("flagged",))
    assert list(back.columns) == ["wind_kt", "dir_deg"] and len(back) == 2 and back.index[1] == t[2]


def test_empty_inputs_encode_with_a_null_start():
    assert series.encode(pd.Series(dtype=float), "m", 2)["t0"] is None
    assert series.decode({"t0": None, "values": []}).empty
    assert series.decode_frame(series.encode_frame(pd.DataFrame())).empty


def test_identify_names_the_caller_on_both_clients(monkeypatch):
    monkeypatch.setattr(coops, "USER_AGENT", coops.USER_AGENT)
    monkeypatch.setattr(coops, "APPLICATION", coops.APPLICATION)
    monkeypatch.setitem(coops._session.headers, "User-Agent", coops._session.headers["User-Agent"])
    assert coops.USER_AGENT.startswith("insitukit/")
    insitukit.identify("example-site/1.0 (+https://example.org)", application="example-site")
    assert coops._session.headers["User-Agent"] == "example-site/1.0 (+https://example.org)"
    assert coops.APPLICATION == "example-site"
    assert erddap.Erddap("https://erddap.example.org/erddap/")._session.headers["User-Agent"].startswith("example-site/")
    assert erddap.Erddap("https://x", user_agent="other")._session.headers["User-Agent"] == "other"


def test_an_empty_erddap_window_is_an_empty_frame(monkeypatch):
    server = erddap.Erddap("https://erddap.example.org/erddap", min_interval=0)
    monkeypatch.setattr(server, "_get", lambda url: None)
    df = server.fetch("ds", datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc), ["temperature"])
    assert list(df.columns) == ["time", "temperature"] and df.empty


def test_the_erddap_budget_stops_requests():
    server = erddap.Erddap("https://erddap.example.org/erddap", budget=0)
    with pytest.raises(erddap.BudgetExceeded):
        server.fetch("ds", datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc), ["t"])
