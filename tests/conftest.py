"""合成 ERA5 netCDF 工厂——零真实网络。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from climate_reanalysis.period import month_hours
from climate_reanalysis.store import TARGET_LAT, TARGET_LON


def make_raw_nc(path: Path, month: str, cds_var: str, value: float = 3600.0,
                lat=None, lon=None, time=None, expver=None) -> Path:
    """按 CDS single-levels 的原生形态写一个合成 netCDF。

    坐标名 valid_time/latitude/longitude，纬度**降序**（CDS 原生如此），
    以便测试 restore 是否正确翻转到契约要求的升序。

    ``time`` 默认等于该月的契约整点轴；显式传入可以构造"步数对但时刻错位"
    这种病态输入，专测 restore 是否真的校验了时间坐标的值而不只是数量。

    ``expver`` 模拟 CDS 原生的 ERA5T/final 成色标记坐标（真实数据 dims
    ``('valid_time',)``、dtype ``<U4``，逐时刻取值，长度须与 ``t`` 一致）；
    默认 ``None`` 不建该坐标，模拟老文件/不产出该坐标的来源。
    """
    t = np.asarray(time if time is not None else month_hours(month))
    la = np.asarray(lat if lat is not None else TARGET_LAT)[::-1]   # 降序
    lo = np.asarray(lon if lon is not None else TARGET_LON)
    data = np.full((t.size, la.size, lo.size), value, dtype="float32")
    coords = {"valid_time": t, "latitude": la, "longitude": lo}
    if expver is not None:
        coords["expver"] = ("valid_time", np.asarray(expver, dtype="<U4"))
    ds = xr.Dataset(
        {cds_var: (("valid_time", "latitude", "longitude"), data)},
        coords=coords,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(path)
    return path


@pytest.fixture
def raw_nc():
    return make_raw_nc
