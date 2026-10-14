"""月份枚举与「整月齐了才发布」的就绪判据。

ERA5 有约 5 天发布延迟，最后一日的小时数据不能在 D+5 凌晨假定已经齐全。
等到下月首日 + ``lag_days``，再由原始文件的时间轴确认完整。半个月不发布——
下游因此永远看到"要么没有、要么完整"，
无需处理尾部 NaN 的模糊语义（spec D4）。

``today`` 由调用方传入而非内部取时钟，使就绪判据在测试里可精确断言。
"""

from __future__ import annotations

import datetime as dt

import numpy as np


def _next_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def month_hours(month: str) -> np.ndarray:
    """该月的原生整点 UTC 时间轴（``YYYYMM``）。"""
    y, m = int(month[:4]), int(month[4:6])
    start = np.datetime64(f"{y:04d}-{m:02d}-01T00:00", "ns")
    ny, nm = _next_month(y, m)
    n = int(
        (np.datetime64(f"{ny:04d}-{nm:02d}-01T00:00", "ns") - start)
        / np.timedelta64(1, "h")
    )
    return start + np.arange(n) * np.timedelta64(1, "h")


def is_ready(month: str, today: dt.date, lag_days: int) -> bool:
    """该月是否可以尝试整月请求（最后一日的滞后窗口已完整经过）。"""
    y, m = int(month[:4]), int(month[4:6])
    ny, nm = _next_month(y, m)
    first_of_next = dt.date(ny, nm, 1)
    return today >= first_of_next + dt.timedelta(days=lag_days)


def ready_months(start: str, today: dt.date, lag_days: int) -> list[str]:
    """从 ``start``（含）到最后一个已齐全月份的连续列表。"""
    y, m = int(start[:4]), int(start[4:6])
    out: list[str] = []
    while True:
        token = f"{y:04d}{m:02d}"
        if not is_ready(token, today, lag_days):
            return out
        out.append(token)
        y, m = _next_month(y, m)
