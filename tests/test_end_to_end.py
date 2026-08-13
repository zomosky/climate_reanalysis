"""端到端：download（打桩）→ restore → 用 zrread 的读取方式验证。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import xarray as xr

from climate_reanalysis.download import download_many
from climate_reanalysis.restore import scan_once
from climate_reanalysis.settings import Settings
from climate_reanalysis.store import store_path
from tests.conftest import make_raw_nc

TODAY = dt.date(2026, 4, 10)


class FakeClient:
    pass


def test_two_months_three_variables_roundtrip(monkeypatch, tmp_path: Path):
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "m",
                 store_dir=tmp_path / "s")
    values = {"surface_solar_radiation_downwards": 7200.0,
              "2m_temperature": 285.0, "surface_pressure": 101325.0}

    def rv(cl, dataset, request, target, proxy, attempts=6, job_ids=None):
        v = request["variable"][0]
        month = f"{request['year'][0]}{request['month'][0]}"
        make_raw_nc(Path(target), month, v, values[v])
        return Path(target)

    monkeypatch.setattr("climate_reanalysis.download.retrieve", rv)

    ok, failed = download_many(s, FakeClient(), ["202601", "202602"],
                               ["ssrd", "t2m", "sp"], TODAY)
    assert not failed and len(ok) == 6
    assert scan_once(s) == (6, 0, 0)

    for month, hours in (("202601", 31 * 24), ("202602", 28 * 24)):
        ds = xr.open_zarr(store_path(s.store_dir, month), consolidated=True)
        assert ds.sizes["time"] == hours
        np.testing.assert_allclose(ds["ssrd"].values[0, 0, 0], 2.0)   # 7200/3600
        np.testing.assert_allclose(ds["t2m"].values[0, 0, 0], 285.0)
        np.testing.assert_allclose(ds["sp"].values[0, 0, 0], 101325.0)
        assert np.isnan(ds["ssrdc"].values).all()      # 未下的变量保持 NaN

    # 再扫一次全部跳过——幂等
    assert scan_once(s) == (0, 6, 0)
