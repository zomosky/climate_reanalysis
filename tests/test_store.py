"""月库：产出契约 + 溯源属性（幂等的载体）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr

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
