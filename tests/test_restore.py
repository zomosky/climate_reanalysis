"""restore：幂等（逐变量溯源比对）、换算、网格与时间轴对齐。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from climate_reanalysis.archive import cold_path
from climate_reanalysis.manifest import manifest_path, write_manifest
from climate_reanalysis.period import month_hours
from climate_reanalysis.restore import restore_one, scan_once
from climate_reanalysis.settings import Settings
from climate_reanalysis.store import TARGET_LAT, TARGET_LON, read_provenance, store_path
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


def _make_raw_nc_with_data_var_names(path: Path, month: str, names: list[str],
                                     value: float = 3600.0) -> Path:
    """同 conftest.make_raw_nc 的原生形态（valid_time、纬度降序），但数据变量名
    可以跟请求的 cds 变量名不一致、也可以放不止一个——用来模拟真实 CDS 文件
    （GRIB shortName，如 't'/'u'，从不等于 cds_var 长名）与"文件里混进第二个
    数据变量"这两种 load_raw 的 fallback 分支场景。"""
    t = month_hours(month)
    lat = TARGET_LAT[::-1]
    lon = TARGET_LON
    shape = (t.size, lat.size, lon.size)
    data = {name: (("valid_time", "latitude", "longitude"),
                   np.full(shape, value, dtype="float32")) for name in names}
    ds = xr.Dataset(data, coords={"valid_time": t, "latitude": lat, "longitude": lon})
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(path)
    return path


def test_load_raw_uses_sole_data_var_when_name_is_a_grib_shortname(tmp_path: Path):
    """真实 CDS netCDF 的数据变量名是 GRIB shortName（如 t2m 对应的长名
    2m_temperature，文件里的变量却叫 't2m' 甚至更短的 't'），从不等于
    cds_var 长名——第一个分支（cds_var in ds.data_vars）在真实数据上永远不
    命中，永远走 fallback。唯一一个数据变量时应正常按位置取用，不能因为
    名字不完全匹配就报错。"""
    s = _settings(tmp_path)
    f = _make_raw_nc_with_data_var_names(
        s.raw_dir / "202601" / "2m_temperature.nc", "202601", ["t2m"], value=280.0)
    mf = write_manifest(s.manifest_dir, "202601", "t2m", [f], AREA)
    assert restore_one(s, mf) == "done"
    ds = xr.open_zarr(store_path(s.store_dir, "202601"), consolidated=True)
    np.testing.assert_allclose(ds["t2m"].values[0, 0, 0], 280.0)


def test_load_raw_rejects_ambiguous_multi_data_var_file(tmp_path: Path):
    """fallback 分支（真实数据的唯一路径）此前既没测试覆盖、也不校验数量：
    文件里若有第二个数据变量，会静默取 list(ds.data_vars)[0]，取错变量却
    没有任何报错。必须报错点名有哪些数据变量。"""
    s = _settings(tmp_path)
    f = _make_raw_nc_with_data_var_names(
        s.raw_dir / "202601" / "2m_temperature.nc", "202601", ["t2m", "extra"])
    mf = write_manifest(s.manifest_dir, "202601", "t2m", [f], AREA)
    with pytest.raises(ValueError, match="数据变量"):
        restore_one(s, mf)
    assert not store_path(s.store_dir, "202601").exists()


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


def test_provenance_records_expver_sorted_unique(tmp_path: Path):
    """expver 逐时刻取值；跨 ERA5/ERA5T 边界的月份会混合两种，必须都记下来。"""
    s = _settings(tmp_path)
    f = make_raw_nc(s.raw_dir / "202601" / "2m_temperature.nc", "202601",
                    "2m_temperature", expver=["0005"] * 372 + ["0001"] * 372)
    mf = write_manifest(s.manifest_dir, "202601", "t2m", [f], AREA)
    assert restore_one(s, mf) == "done"
    assert read_provenance(s.store_dir, "202601")["t2m"]["expver"] == ["0001", "0005"]


def test_provenance_expver_empty_when_coordinate_absent(tmp_path: Path):
    """没有 expver 坐标的文件不该让整条管线报错——记空列表即可。"""
    s, mf = _prepare(tmp_path, month="202601", var="t2m", cds="2m_temperature", value=280.0)
    assert restore_one(s, mf) == "done"
    assert read_provenance(s.store_dir, "202601")["t2m"]["expver"] == []


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


def test_rebuild_falls_back_to_cold_copy(tmp_path: Path):
    """月库丢了要重建，而热端 raw 已归档——能从冷端读回来，不必重下。"""
    # 构造：manifest 在、月库无溯源（模拟月库丢失）、热端 raw 不存在、冷端有副本
    month, cds = "202601", "surface_solar_radiation_downwards"
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "m",
                store_dir=tmp_path / "s", cold_dir=tmp_path / "cold")
    f = make_raw_nc(s.raw_dir / month / f"{cds}.nc", month, cds, 3600.0)
    mf = write_manifest(s.manifest_dir, month, "ssrd", [f], AREA)

    # 模拟「已归档」：内容原样搬到冷端对应位置（<cold_dir>/<month>/<basename>），
    # 删掉热端原文件——不经过 archive() 本身，只构造 load_raw 要看到的落盘状态。
    cold = cold_path(s.cold_dir, month, f.name)
    cold.parent.mkdir(parents=True)
    cold.write_bytes(f.read_bytes())
    f.unlink()

    # 断言：restore 成功、月库数值正确（不必重下——从冷端读回来自愈）
    assert restore_one(s, mf) == "done"
    ds = xr.open_zarr(store_path(s.store_dir, month), consolidated=True)
    np.testing.assert_allclose(ds["ssrd"].values[0, 0, 0], 1.0)   # 3600/3600


def test_missing_raw_without_cold_dir_raises(tmp_path: Path):
    """cold_dir 未配置（出厂 None）时行为不变——热端缺失直接报错，不会误报
    "成功"，也不会因为新逻辑而意外吞掉这种缺失。"""
    s, mf = _prepare(tmp_path)
    (s.raw_dir / "202601" / "surface_solar_radiation_downwards.nc").unlink()
    with pytest.raises(FileNotFoundError):
        restore_one(s, mf)


def test_missing_raw_and_missing_cold_copy_raises(tmp_path: Path):
    """两处都没有才报错：配置了 cold_dir，但冷端也没有这份文件——不能假装
    找到了，必须点名报错。"""
    month, cds = "202601", "surface_solar_radiation_downwards"
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "m",
                store_dir=tmp_path / "s", cold_dir=tmp_path / "cold")
    f = make_raw_nc(s.raw_dir / month / f"{cds}.nc", month, cds, 3600.0)
    mf = write_manifest(s.manifest_dir, month, "ssrd", [f], AREA)
    f.unlink()   # 热端缺失，冷端目录甚至没建过

    with pytest.raises(FileNotFoundError):
        restore_one(s, mf)
