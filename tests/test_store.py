"""月库：产出契约 + 溯源属性（幂等的载体）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr
import zarr

from climate_reanalysis.store import (
    CHUNK,
    SHARD,
    TARGET_LAT,
    TARGET_LON,
    ensure_store,
    read_provenance,
    set_provenance,
    store_path,
    write_variable,
)


def test_grid_matches_zrread_contract():
    """网格必须与 zrread 已在读的月库、以及 ifs-hres 逐格相同。"""
    assert TARGET_LAT.size == 161 and TARGET_LON.size == 281
    assert TARGET_LAT[0] == 15.0 and TARGET_LAT[-1] == 55.0
    assert TARGET_LON[0] == 70.0 and TARGET_LON[-1] == 140.0


def test_ensure_store_creates_all_nine_variables_as_nan(tmp_path: Path):
    p = ensure_store(tmp_path, "202601")
    ds = xr.open_zarr(p, consolidated=True)
    assert set(ds.data_vars) == {
        "ssrd", "fdir", "ssrdc", "t2m", "d2m", "si10", "u100", "v100", "sp"
    }
    assert ds.sizes == {"time": 31 * 24, "latitude": 161, "longitude": 281}
    assert np.isnan(ds["ssrd"].values).all()
    enc = ds["ssrd"].encoding
    assert tuple(enc["chunks"]) == (31 * 24, CHUNK, CHUNK)
    assert tuple(enc["shards"]) == (31 * 24, SHARD, SHARD)


def test_ensure_store_is_idempotent(tmp_path: Path):
    a = ensure_store(tmp_path, "202601")
    set_provenance(tmp_path, "202601", "ssrd", {"manifest_sha256": "abc"})
    b = ensure_store(tmp_path, "202601")
    assert a == b
    assert read_provenance(tmp_path, "202601")["ssrd"]["manifest_sha256"] == "abc"


def test_ensure_store_rebuilds_a_skeleton_left_incomplete_by_a_crash(tmp_path: Path):
    """建骨架途中崩溃的现实后果（审查实测）：库里只有 t2m 一个数组、没有
    time/latitude/longitude 坐标，却被后续 write_variable 自己的 consolidate
    标成「完整」——path.exists() 为真，永远不会被重建。

    这里直接构造这种残缺状态（只建一个数组就 consolidate，模拟"崩溃前只做完
    这一步"），断言 ensure_store 必须识别出它不完整并整体重建，而不是原样放行。
    """
    p = store_path(tmp_path, "202601")
    g = zarr.open_group(str(p), mode="w")
    g.create_array("t2m", shape=(31 * 24, 161, 281), dtype="float32")
    zarr.consolidate_metadata(g.store)   # 残缺状态被就地"标记"成看似完整

    # 修复前：path.exists() 为真就直接放行，下面的断言会在残缺库上失败。
    result = ensure_store(tmp_path, "202601")
    assert result == p

    ds = xr.open_zarr(p, consolidated=True)
    assert set(ds.data_vars) == {
        "ssrd", "fdir", "ssrdc", "t2m", "d2m", "si10", "u100", "v100", "sp"
    }
    assert ds.sizes == {"time": 31 * 24, "latitude": 161, "longitude": 281}
    assert np.isnan(ds["t2m"].values).all()   # 重建后是全 NaN 骨架，不是残缺数组里的垃圾值


def test_write_variable_lands_values_and_provenance(tmp_path: Path):
    ensure_store(tmp_path, "202601")
    data = np.full((31 * 24, 161, 281), 3.5, dtype="float32")
    write_variable(tmp_path, "202601", "t2m", data, {"manifest_sha256": "deadbeef"})
    ds = xr.open_zarr(store_path(tmp_path, "202601"), consolidated=True)
    np.testing.assert_allclose(ds["t2m"].values[0, 0, 0], 3.5)
    assert np.isnan(ds["ssrd"].values).all()          # 只动指定变量
    prov = read_provenance(tmp_path, "202601")
    assert prov["t2m"]["manifest_sha256"] == "deadbeef"
    assert "ssrd" not in prov


def test_write_variable_rejects_wrong_shape(tmp_path: Path):
    ensure_store(tmp_path, "202601")
    import pytest

    with pytest.raises(ValueError, match="形状"):
        write_variable(tmp_path, "202601", "t2m",
                       np.zeros((10, 161, 281), dtype="float32"), {})


def test_set_provenance_preserves_other_variables(tmp_path: Path):
    """set_provenance 是读-改-写：更新一个变量不得冲掉其它变量已有的条目。"""
    ensure_store(tmp_path, "202601")
    set_provenance(tmp_path, "202601", "ssrd", {"manifest_sha256": "sha_ssrd"})
    set_provenance(tmp_path, "202601", "t2m", {"manifest_sha256": "sha_t2m"})
    prov = read_provenance(tmp_path, "202601")
    assert prov["ssrd"]["manifest_sha256"] == "sha_ssrd"
    assert prov["t2m"]["manifest_sha256"] == "sha_t2m"

    # 再更新其中一个，另一个的条目原样保留（不是被新写入覆盖或清空）
    set_provenance(tmp_path, "202601", "ssrd", {"manifest_sha256": "sha_ssrd_v2"})
    prov2 = read_provenance(tmp_path, "202601")
    assert prov2["ssrd"]["manifest_sha256"] == "sha_ssrd_v2"
    assert prov2["t2m"]["manifest_sha256"] == "sha_t2m"


def test_write_variable_preserves_other_variables_provenance(tmp_path: Path):
    """连续 write_variable 两个不同变量，两条溯源都要在——不是后写的顶替前写的。"""
    ensure_store(tmp_path, "202601")
    data = np.full((31 * 24, 161, 281), 1.0, dtype="float32")
    write_variable(tmp_path, "202601", "ssrd", data, {"manifest_sha256": "sha_ssrd"})
    write_variable(tmp_path, "202601", "t2m", data, {"manifest_sha256": "sha_t2m"})
    prov = read_provenance(tmp_path, "202601")
    assert prov["ssrd"]["manifest_sha256"] == "sha_ssrd"
    assert prov["t2m"]["manifest_sha256"] == "sha_t2m"


def test_consolidated_metadata_refreshed_after_write(tmp_path: Path):
    """写入后 consolidated 元数据必须重写——否则新变量对 xarray 不可见。

    （它同时是"完成标记"，写入顺序由实现保证；此处断言的是刷新这件事本身。）
    """
    ensure_store(tmp_path, "202601")
    data = np.full((31 * 24, 161, 281), 7.0, dtype="float32")
    write_variable(tmp_path, "202601", "t2m", data, {"manifest_sha256": "x"})
    ds = xr.open_zarr(store_path(tmp_path, "202601"), consolidated=True)
    np.testing.assert_allclose(ds["t2m"].values[0, 0, 0], 7.0)


def test_write_variable_consolidates_only_after_data_is_on_disk(tmp_path: Path, monkeypatch):
    """完整性标志（consolidated 元数据）必须最后写：崩溃在它之前的库不能被误判
    完整。这里不满足于事后检查最终状态——那测不出顺序错了，只测得出最终结果对了
    （错误顺序在无崩溃的正常路径下产出的最终文件内容其实是一样的）。真正钉住
    顺序的办法是在 consolidate 被调用的**那一刻**回头看数据是不是已经在盘上：
    打桩 zarr.consolidate_metadata，在桩里用一个独立的（非 consolidated）group
    句柄直接读目标值——如果实现把 consolidate 挪到写数据之前，这个瞬间读到的
    还是 NaN 骨架值，断言就会失败。"""
    ensure_store(tmp_path, "202601")
    data = np.full((31 * 24, 161, 281), 9.0, dtype="float32")
    seen: dict = {}
    real_consolidate = zarr.consolidate_metadata

    def spy(store, *a, **kw):
        g = zarr.open_group(str(store_path(tmp_path, "202601")), mode="r")
        seen["value_at_consolidate_time"] = float(g["t2m"][0, 0, 0])
        return real_consolidate(store, *a, **kw)

    monkeypatch.setattr(zarr, "consolidate_metadata", spy)
    write_variable(tmp_path, "202601", "t2m", data, {"manifest_sha256": "x"})

    assert seen["value_at_consolidate_time"] == 9.0
