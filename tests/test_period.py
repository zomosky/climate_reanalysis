"""月份枚举与就绪判据。"""

from __future__ import annotations

import datetime as dt

import numpy as np

from climate_reanalysis.period import is_ready, month_hours, ready_months


def test_month_hours_native_integer_axis():
    t = month_hours("202601")
    assert t.size == 31 * 24
    assert t[0] == np.datetime64("2026-01-01T00:00")
    assert t[-1] == np.datetime64("2026-01-31T23:00")
    assert np.all(np.diff(t) == np.timedelta64(1, "h"))


def test_month_hours_handles_february_and_leap_year():
    assert month_hours("202602").size == 28 * 24
    assert month_hours("202402").size == 29 * 24


def test_not_ready_before_month_end_plus_lag():
    # 2026-01 要到 2026-02-05 才算齐（月末 + 5 天）
    assert not is_ready("202601", dt.date(2026, 2, 4), 5)
    assert is_ready("202601", dt.date(2026, 2, 5), 5)


def test_current_month_never_ready():
    assert not is_ready("202602", dt.date(2026, 2, 20), 5)


def test_ready_months_stops_at_the_last_complete_month():
    got = ready_months("202511", dt.date(2026, 2, 5), 5)
    assert got == ["202511", "202512", "202601"]


def test_ready_months_empty_when_nothing_complete():
    assert ready_months("202602", dt.date(2026, 2, 20), 5) == []
