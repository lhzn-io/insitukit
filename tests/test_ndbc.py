"""NDBC: file naming, parsing, sentinels, the source fallback, currents and positions. Offline: files are served by
an injected ``get``, shaped like NDBC's April 2026 files for buoy 44091."""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from insitukit import ndbc

STDMET = """#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS  TIDE
#yr  mo dy hr mn degT m/s  m/s     m   sec   sec deg    hPa  degC  degC  degC  nmi    ft
2026 04 01 23 30 999 99.0 99.0  1.94  7.14  5.13 176 9999.0   8.5   5.9 999.0 99.0 99.00
2026 04 02 00 00 200  6.0  8.0  1.82  7.41  5.14 170 1020.4   9.2   6.0 999.0 99.0 99.00
2026 04 02 00 30 210  7.0  9.0    MM  7.41  5.14 170 1020.6   9.0   6.1 999.0 99.0 99.00
2026 04 03 00 00 220  8.0 10.0  1.50  7.00  5.00 170 1021.0   8.0   6.2 999.0 99.0 99.00
"""

ADCP = """#YY  MM DD hh mm DEP01 DIR01 SPD01
#yr  mo dy hr mn     m   deg  cm/s
2026 04 02 00 00     1.0    90  20.0
2026 04 02 00 30     1.0   180  10.0
"""

UTC = timezone.utc


def served(files: dict[str, str], asked: list[str] | None = None):
    def get(url):
        if asked is not None:
            asked.append(url)
        for key, text in files.items():
            if key in url:
                return text
        return None

    return get


def test_file_names_follow_ndbc():
    assert [ndbc.month_code(m) for m in (1, 9, 10, 12)] == ["1", "9", "a", "c"]
    u = ndbc.urls("44091", "stdmet", 2026, 4)
    assert u[0].endswith("/data/stdmet/Apr/4409142026.txt.gz")
    assert u[1].endswith("/data/historical/stdmet/44091h2026.txt.gz")
    assert u[2].endswith("/data/realtime2/44091.txt")
    assert ndbc.urls("44091", "adcp", 2025, 11)[0].endswith("/data/adcp/Nov/44091b2025.txt.gz")
    with pytest.raises(ValueError):
        ndbc.urls("44091", "waves", 2026, 1)


def test_parse_maps_sentinels_and_mm_to_nan_and_keeps_units():
    df = ndbc.parse(STDMET)
    assert df["time"].iloc[1] == pd.Timestamp("2026-04-02T00:00", tz="UTC")
    assert np.isnan(df["WSPD"].iloc[0]) and np.isnan(df["PRES"].iloc[0]) and np.isnan(df["WVHT"].iloc[2])
    assert df["WTMP"].iloc[1] == 6.0 and df.attrs["units"]["WTMP"] == "degC"


def test_sentinels_are_per_column_so_real_readings_survive():
    text = ("#YY  MM DD hh mm WDIR WSPD PRES ATMP DIR01 SPD01\n#yr mo dy hr mn degT m/s hPa degC deg cm/s\n"
            "2005 01 23 05 00 99 10.3 999.0 99.0 99 99.0\n2005 01 23 06 00 999 99.0 9999.0 999.0 999 999.0\n")
    df = ndbc.parse(text)
    # A 99-degree wind, a 999 hPa low, a 99 cm/s current: real readings, kept.
    assert df.iloc[0][["WDIR", "PRES", "ATMP", "DIR01", "SPD01"]].tolist() == [99.0, 999.0, 99.0, 99.0, 99.0]
    assert df.iloc[1].drop("time").isna().all()


def test_old_files_two_digit_years_and_old_names():
    old = "YY MM DD hh WD WSPD BAR ATMP\n98 04 02 00 200 6.0 1020.4 5.5\n"
    df = ndbc.parse(old)
    assert df["time"].iloc[0] == pd.Timestamp("1998-04-02T00:00", tz="UTC")
    assert list(df.columns) == ["time", "WDIR", "WSPD", "PRES", "ATMP"]
    df = ndbc.parse("YYYY MM DD hh WD WSPD BAR\n2004 01 01 00 999 99.0 9999.0\n")
    assert df["time"].iloc[0].year == 2004 and df[["WDIR", "WSPD", "PRES"]].isna().all(axis=None)


def test_fetch_falls_back_and_downloads_each_file_once():
    asked: list[str] = []
    get = served({"historical/stdmet": STDMET}, asked)
    df = ndbc.fetch("44091", datetime(2026, 3, 31, tzinfo=UTC), datetime(2026, 4, 3, tzinfo=UTC), get=get)
    # [start, end): the 04-03 00:00 row is out; March and April both come from the one yearly file.
    assert df["time"].dt.strftime("%m-%d %H:%M").tolist() == ["04-01 23:30", "04-02 00:00", "04-02 00:30"]
    assert sum("historical" in u for u in asked) == 1
    assert df.attrs["sources"] == ["https://www.ndbc.noaa.gov/data/historical/stdmet/44091h2026.txt.gz"]


def test_currents_are_metres_per_second_toward():
    get = served({"historical/adcp": ADCP})
    df = ndbc.currents("44091", datetime(2026, 4, 2, tzinfo=UTC), datetime(2026, 4, 3, tzinfo=UTC), get=get)
    assert df["speed"].tolist() == [0.2, 0.1] and df["depth"].iloc[0] == 1.0
    assert df["u"].iloc[0] == pytest.approx(0.2) and df["v"].iloc[0] == pytest.approx(0.0, abs=1e-12)
    assert df["v"].iloc[1] == pytest.approx(-0.1)


def test_year_reads_the_historical_file_or_none():
    get = served({"historical/stdmet/44091h2026": STDMET})
    assert len(ndbc.year("44091", 2026, get=get)) == 4
    assert ndbc.year("44091", 2025, get=get) is None


def test_position_from_the_station_page():
    html = "<b>39.772 N 73.769 W (39&#176;46'19\" N 73&#176;46'8\" W)</b>"
    assert ndbc.position("44091", get=lambda url: html) == (39.772, -73.769)
    with pytest.raises(ValueError):
        ndbc.position("00000", get=lambda url: None)


def test_requests_are_paced_identified_and_404_is_none(monkeypatch):
    from insitukit import coops

    sent = []

    class Resp:
        def __init__(self, status, text=""):
            self.status_code, self.text, self.content = status, text, text.encode()

    def fake(url, timeout=None, headers=None):
        sent.append(headers["User-Agent"])
        return Resp(404) if "missing" in url else Resp(200, "ok")

    monkeypatch.setattr(ndbc._session, "get", fake)
    monkeypatch.setattr(ndbc, "MIN_INTERVAL", 0.0)
    assert ndbc._get("https://x/missing.txt") is None and ndbc._get("https://x/ok.txt") == "ok"
    assert sent == [coops.USER_AGENT, coops.USER_AGENT]
