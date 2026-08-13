"""restore：幂等（逐变量溯源比对）、换算、网格与时间轴对齐。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from climate_reanalysis.manifest import manifest_path, write_manifest
from climate_reanalysis.period import month_hours
from climate_reanalysis.restore import restore_one, scan_once
from climate_reanalysis.settings import Settings
from climate_reanalysis.store import TARGET_LAT, read_provenance, store_path
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


def test_second_run_skips_when_provenance_matches(tmp_path: Path, monkeypatch):
    s, mf = _prepare(tmp_path)
    assert restore_one(s, mf) == "done"

    # 独立证据：不只信返回值自己的报告——真正断言"没有重写"这件事本身，
    # 打桩 write_variable 记录调用，"skip" 路径必须一次都不碰它。
    calls: list = []
    monkeypatch.setattr("climate_reanalysis.restore.write_variable",
                        lambda *a, **k: calls.append((a, k)))
    assert restore_one(s, mf) == "skip"
    assert calls == []


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


def test_scan_once_counts_and_is_idempotent(tmp_path: Path, monkeypatch):
    s, _ = _prepare(tmp_path)
    assert scan_once(s) == (1, 0, 0)

    # 同上：独立证据而非只信计数——第二趟必须一次都没有重写。
    calls: list = []
    monkeypatch.setattr("climate_reanalysis.restore.write_variable",
                        lambda *a, **k: calls.append((a, k)))
    assert scan_once(s) == (0, 1, 0)
    assert calls == []


def test_time_axis_mismatch_is_rejected_not_silently_written(tmp_path: Path):
    """步数凑巧对但时刻错位（缺一格、末尾多一格）：必须抛错，绝不能静默写入
    时间平移过的数据——这是下游直接读的库，静默错位没有任何征兆。"""
    s = _settings(tmp_path)
    month = "202601"
    t = month_hours(month)
    shifted = np.concatenate([t[1:], t[-1:] + np.timedelta64(1, "h")])
    assert shifted.size == t.size and not np.array_equal(shifted, t)
    f = make_raw_nc(s.raw_dir / month / "surface_solar_radiation_downwards.nc",
                    month, "surface_solar_radiation_downwards", 3600.0, time=shifted)
    mf = write_manifest(s.manifest_dir, month, "ssrd", [f], AREA)

    with pytest.raises(ValueError, match="时间"):
        restore_one(s, mf)
    # 拒绝必须发生在写入之前——月库压根不该被建出来
    assert not store_path(s.store_dir, month).exists()


def test_si10_grid_mismatch_between_two_files_is_rejected(tmp_path: Path):
    """si10 由 u10/v10 两个文件合成，两个文件的网格都必须核对契约——不能只
    核对循环里最后处理的那一个（否则另一个网格错位时 np.hypot 会静默按位置
    合成出错误的风速）。"""
    s = _settings(tmp_path)
    bad_lat = TARGET_LAT + 0.1   # 整体偏移 0.1 度，不再匹配契约网格
    u = make_raw_nc(s.raw_dir / "202601" / "10m_u_component_of_wind.nc",
                    "202601", "10m_u_component_of_wind", 3.0, lat=bad_lat)
    v = make_raw_nc(s.raw_dir / "202601" / "10m_v_component_of_wind.nc",
                    "202601", "10m_v_component_of_wind", 4.0)
    mf = write_manifest(s.manifest_dir, "202601", "si10", [u, v], AREA)

    with pytest.raises(ValueError, match="网格"):
        restore_one(s, mf)
    assert not store_path(s.store_dir, "202601").exists()
