"""restore：幂等（逐变量溯源比对）、换算、网格与时间轴对齐。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr

from climate_reanalysis.manifest import manifest_path, write_manifest
from climate_reanalysis.restore import restore_one, scan_once
from climate_reanalysis.settings import Settings
from climate_reanalysis.store import read_provenance, store_path
from tests.conftest import make_raw_nc

AREA = (55.0, 70.0, 15.0, 140.0)


def _settings(tmp_path: Path) -> Settings:
    return Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "m",
                    store_dir=tmp_path / "s")


def _prepare(tmp_path: Path, month="202601", var="ssrd",
             cds="surface_solar_radiation_downwards", value=3600.0):
    s = _settings(tmp_path)
    f = make_raw_nc(s.raw_dir / month / f"{cds}.nc", month, cds, value)
    mf = write_manifest(s.manifest_dir, month, var, [f], AREA)
    return s, mf


def test_restore_applies_conversion_and_ascending_latitude(tmp_path: Path):
    """J/m² ÷3600；CDS 原生纬度降序须翻成契约要求的升序。"""
    s, mf = _prepare(tmp_path)
    assert restore_one(s, mf) == "done"
    ds = xr.open_zarr(store_path(s.store_dir, "202601"), consolidated=True)
    assert float(ds["latitude"][0]) == 15.0          # 升序
    np.testing.assert_allclose(ds["ssrd"].values[0, 0, 0], 1.0)   # 3600/3600


def test_time_axis_is_native_hours_not_shifted(tmp_path: Path):
    """不得做任何中点搬移——那是 solarcast 为 pvlib 做的。"""
    s, mf = _prepare(tmp_path)
    restore_one(s, mf)
    ds = xr.open_zarr(store_path(s.store_dir, "202601"), consolidated=True)
    assert ds["time"].values[0] == np.datetime64("2026-01-01T00:00")


def test_second_run_skips_when_provenance_matches(tmp_path: Path):
    s, mf = _prepare(tmp_path)
    assert restore_one(s, mf) == "done"
    assert restore_one(s, mf) == "skip"


def test_changed_manifest_rebuilds_only_that_variable(tmp_path: Path):
    s, mf = _prepare(tmp_path, var="ssrd", value=3600.0)
    restore_one(s, mf)
    # 另一个变量先入库
    f2 = make_raw_nc(s.raw_dir / "202601" / "2m_temperature.nc", "202601",
                     "2m_temperature", 288.0)
    mf2 = write_manifest(s.manifest_dir, "202601", "t2m", [f2], AREA)
    restore_one(s, mf2)
    prov_t2m = read_provenance(s.store_dir, "202601")["t2m"]

    # ssrd 的原始数据变了 -> 重写 manifest -> 只重建 ssrd
    make_raw_nc(s.raw_dir / "202601" / "surface_solar_radiation_downwards.nc",
                "202601", "surface_solar_radiation_downwards", 7200.0)
    mf3 = write_manifest(s.manifest_dir, "202601", "ssrd",
                         [s.raw_dir / "202601" / "surface_solar_radiation_downwards.nc"],
                         AREA)
    assert restore_one(s, mf3) == "done"
    ds = xr.open_zarr(store_path(s.store_dir, "202601"), consolidated=True)
    np.testing.assert_allclose(ds["ssrd"].values[0, 0, 0], 2.0)
    np.testing.assert_allclose(ds["t2m"].values[0, 0, 0], 288.0)   # 未受影响
    assert read_provenance(s.store_dir, "202601")["t2m"] == prov_t2m


def test_si10_hypot_from_two_files(tmp_path: Path):
    s = _settings(tmp_path)
    u = make_raw_nc(s.raw_dir / "202601" / "10m_u_component_of_wind.nc",
                    "202601", "10m_u_component_of_wind", 3.0)
    v = make_raw_nc(s.raw_dir / "202601" / "10m_v_component_of_wind.nc",
                    "202601", "10m_v_component_of_wind", 4.0)
    mf = write_manifest(s.manifest_dir, "202601", "si10", [u, v], AREA)
    restore_one(s, mf)
    ds = xr.open_zarr(store_path(s.store_dir, "202601"), consolidated=True)
    np.testing.assert_allclose(ds["si10"].values[0, 0, 0], 5.0)


def test_scan_once_counts_and_is_idempotent(tmp_path: Path):
    s, _ = _prepare(tmp_path)
    assert scan_once(s) == (1, 0, 0)
    assert scan_once(s) == (0, 1, 0)
