"""Read only time coordinates before a transport result becomes a ready file."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr

from climate_reanalysis.period import month_hours


class IncompleteMonthError(ValueError):
    """A readable CDS result contains a strict subset of the requested hours."""

    def __init__(self, month: str, files: dict):
        self.files = files
        name, report = next(iter(files.items()))
        super().__init__(
            f'{month}: CDS 原始月份不完整，{len(files)} 个文件缺时次；'
            f'{name}: {report["observed_hours"]}/{report["expected_hours"]} 小时，'
            f'缺失范围 {report["first_missing"]} 至 {report["last_missing"]} UTC')


def time_axis_report(path: Path, month: str) -> dict | None:
    """Return missing-hour evidence; reject duplicates or unexpected timestamps.

    A byte-complete, valid NetCDF can still be a partial ERA5T response. This
    reads coordinates only, leaving full-grid/value validation to publication.
    """
    expected = month_hours(month)
    with xr.open_dataset(path) as ds:
        key = 'valid_time' if 'valid_time' in ds.coords else 'time'
        if key not in ds.coords:
            raise ValueError(f'{month}/{path.stem}: 原始文件没有时间坐标')
        actual = np.asarray(ds[key].values)
    if actual.ndim != 1 or not np.issubdtype(actual.dtype, np.datetime64):
        raise ValueError(f'{month}/{path.stem}: 原始时间坐标格式不符')
    actual = np.sort(actual.astype('datetime64[ns]'))
    if np.array_equal(actual, expected):
        return None
    # Only genuine missing hours trigger a fresh CDS result. Offsets, duplicate
    # labels, or a wrong month may be a contract/code error, so fail closed.
    if (np.unique(actual).size != actual.size
            or not np.isin(actual, expected).all()):
        raise ValueError(f'{month}/{path.stem}: 时间轴有重复或契约外时刻，拒绝自动重下')
    missing = np.setdiff1d(expected, actual)
    return {
        'observed_hours': int(actual.size),
        'expected_hours': int(expected.size),
        'missing_hours': int(missing.size),
        'first_missing': str(np.datetime_as_string(missing[0], unit='h')),
        'last_missing': str(np.datetime_as_string(missing[-1], unit='h')),
    }
