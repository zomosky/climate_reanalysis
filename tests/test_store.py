"""月库：产出契约 + 溯源属性（幂等的载体）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr
import zarr

import pytest

from climate_reanalysis.store import (
    CHUNK,
    SHARD,
    TARGET_LAT,
    TARGET_LON,
    IncompleteStoreHasDataError,
    StoreCorruptedError,
    check_area_matches_grid,
    ensure_store,
    has_data,
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


def test_check_area_matches_grid_accepts_the_matching_area():
    check_area_matches_grid((55.0, 70.0, 15.0, 140.0))   # N, W, S, E——不报错


def test_check_area_matches_grid_rejects_drifted_area():
    """area 与 TARGET_LAT/TARGET_LON 是两份独立的真值，没有互相校验就会各自
    漂移——改错 area 不会在下载时报错，要等整月下载完进 restore 才报"网格
    不符"，代价是白白花掉的 CDS 配额和数小时带宽。这里在真的提交任何请求
    之前就把配置错误拦下来。"""
    with pytest.raises(ValueError, match="网格"):
        check_area_matches_grid((55.0, 70.0, 16.0, 140.0))   # S 打错了一度


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


def test_ensure_store_rebuilds_an_empty_shell_left_incomplete_by_a_crash(
    tmp_path: Path, capsys
):
    """情况 3b：建骨架途中崩溃，且崩溃前落盘的这一个数组还没来得及写任何真实
    值（``fill_value=np.nan``，跟 ``_build_skeleton`` 真实创建数组时一致）——
    库里只有 t2m 一个全 NaN 数组、没有 time/latitude/longitude 坐标，却被
    consolidate 标成看似「完整」（path.exists() 为真，若只看这一条会永远不
    被重建）。

    这是一个确认过的空壳（无真实数据、无溯源），安全重建；断言 ensure_store
    识别出不完整并整体重建，且打了日志说明重建原因。
    """
    p = store_path(tmp_path, "202601")
    g = zarr.open_group(str(p), mode="w")
    g.create_array("t2m", shape=(31 * 24, 161, 281), dtype="float32", fill_value=np.nan)
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

    out = capsys.readouterr().out
    assert "rebuilding_empty_skeleton" in out and "202601" in out


def test_ensure_store_refuses_to_rebuild_incomplete_skeleton_with_real_data(
    tmp_path: Path
):
    """情况 3a：骨架不完整，但已经写入过真实数据（不是全 NaN 骨架值）——多半是
    在"写完部分变量的真实数据"之后、"补全其余变量/坐标"之前崩溃。此时绝不能
    像情况 3b 那样自动 ``mode="w"`` 重建：那会把这份真实数据静默清空，且不抛
    任何异常、不留任何日志。必须拒绝重建、抛错点名，交给人工核实。"""
    p = store_path(tmp_path, "202601")
    g = zarr.open_group(str(p), mode="w")
    arr = g.create_array("t2m", shape=(31 * 24, 161, 281), dtype="float32",
                         fill_value=np.nan)
    arr[:] = 5.0   # 真实数据，不是骨架 NaN
    zarr.consolidate_metadata(g.store)   # 骨架仍不完整：缺其余 8 个变量、缺三个坐标

    with pytest.raises(IncompleteStoreHasDataError, match="202601"):
        ensure_store(tmp_path, "202601")

    # 核心断言：数据必须原封不动——不能被这次被拒绝的 ensure_store 调用清空。
    g2 = zarr.open_group(str(p), mode="r")
    assert float(g2["t2m"][0, 0, 0]) == 5.0


def test_ensure_store_refuses_to_rebuild_incomplete_skeleton_with_only_provenance(
    tmp_path: Path
):
    """情况 3a 的另一种真实内容信号：数据数组本身还是骨架 NaN（还没来得及写
    真实值），但溯源已经先落盘（``write_variable`` 的写入顺序是数据→溯源→
    consolidated；如果崩溃发生在"溯源写完、但下一个变量的数据还没开始写"这个
    间隙，consolidated 元数据本身甚至可能还没刷新到最新——溯源本身就足以说明
    这不是一个从未使用过的空壳）。仅凭数据数组全 NaN 不能判定"没有真实内容"。
    """
    p = store_path(tmp_path, "202601")
    g = zarr.open_group(str(p), mode="w")
    g.create_array("t2m", shape=(31 * 24, 161, 281), dtype="float32", fill_value=np.nan)
    g.attrs["zrread_provenance"] = {"t2m": {"manifest_sha256": "deadbeef"}}
    zarr.consolidate_metadata(g.store)   # 骨架仍不完整：缺其余变量、缺坐标

    with pytest.raises(IncompleteStoreHasDataError, match="202601"):
        ensure_store(tmp_path, "202601")

    g2 = zarr.open_group(str(p), mode="r")
    assert dict(g2.attrs)["zrread_provenance"]["t2m"]["manifest_sha256"] == "deadbeef"


def test_ensure_store_refuses_to_rebuild_a_corrupted_store(tmp_path: Path):
    """情况 4：库根本打不开（磁盘级损坏）——审查实测场景：健康月库（t2m/sp/ssrd
    都有真实数据+溯源）→ 把 root zarr.json 截断损坏 → 修复前的宽 except 会把
    这种"打不开"误判成"没建完"，进而用 mode="w" 整体重建，把全部真实数据和
    溯源静默清空（不抛异常、不打日志）。

    这里直接复现该场景：先建一个有真实数据+溯源的健康库，截断 root zarr.json，
    断言 ensure_store 拒绝重建并抛错点名；再把 zarr.json 还原，确认底下的数据
    数组、溯源全都原封不动（没有被之前失败的调用动过）。
    """
    ensure_store(tmp_path, "202601")
    data = np.full((31 * 24, 161, 281), 3.0, dtype="float32")
    write_variable(tmp_path, "202601", "t2m", data, {"manifest_sha256": "sha-t2m"})
    write_variable(tmp_path, "202601", "sp", data, {"manifest_sha256": "sha-sp"})

    p = store_path(tmp_path, "202601")
    root_meta = p / "zarr.json"
    original_bytes = root_meta.read_bytes()
    root_meta.write_bytes(b"{not valid json")   # 模拟磁盘级损坏（截断/写坏）

    with pytest.raises(StoreCorruptedError, match="202601"):
        ensure_store(tmp_path, "202601")

    # 还原损坏的元数据文件，证明失败的 ensure_store 调用没有碰过任何数据数组
    # 或其它文件——库里的真实内容跟损坏之前逐字节一致。
    root_meta.write_bytes(original_bytes)
    ds = xr.open_zarr(p, consolidated=True)
    np.testing.assert_allclose(ds["t2m"].values, 3.0)
    np.testing.assert_allclose(ds["sp"].values, 3.0)
    prov = read_provenance(tmp_path, "202601")
    assert prov["t2m"]["manifest_sha256"] == "sha-t2m"
    assert prov["sp"]["manifest_sha256"] == "sha-sp"


def test_write_variable_refuses_to_touch_a_corrupted_store(tmp_path: Path):
    """审查复现的确切场景（``write_variable`` 层面，而不是 ``ensure_store``）：
    健康月库写好 t2m/sp/ssrd 之后损坏，再对同一个库调用 ``write_variable`` 写
    一个新变量 u100——这才是 restore.py 实际调用的入口。必须拒绝写入并抛错，
    不能像修复前那样把其余变量的数据/溯源静默清空、只剩 u100。"""
    ensure_store(tmp_path, "202601")
    data = np.full((31 * 24, 161, 281), 1.0, dtype="float32")
    write_variable(tmp_path, "202601", "t2m", data, {"manifest_sha256": "sha-t2m"})
    write_variable(tmp_path, "202601", "sp", data, {"manifest_sha256": "sha-sp"})
    write_variable(tmp_path, "202601", "ssrd", data, {"manifest_sha256": "sha-ssrd"})

    p = store_path(tmp_path, "202601")
    root_meta = p / "zarr.json"
    original_bytes = root_meta.read_bytes()
    root_meta.write_bytes(b"{not valid json")

    with pytest.raises(StoreCorruptedError, match="202601"):
        write_variable(tmp_path, "202601", "u100",
                       np.full((31 * 24, 161, 281), 9.0, dtype="float32"), {})

    root_meta.write_bytes(original_bytes)
    ds = xr.open_zarr(p, consolidated=True)
    np.testing.assert_allclose(ds["t2m"].values, 1.0)
    np.testing.assert_allclose(ds["sp"].values, 1.0)
    np.testing.assert_allclose(ds["ssrd"].values, 1.0)
    assert np.isnan(ds["u100"].values).all()   # u100 没有被写入——调用在写入前就报错了
    prov = read_provenance(tmp_path, "202601")
    assert prov["t2m"]["manifest_sha256"] == "sha-t2m"
    assert prov["sp"]["manifest_sha256"] == "sha-sp"
    assert prov["ssrd"]["manifest_sha256"] == "sha-ssrd"
    assert "u100" not in prov


def test_has_data_distinguishes_all_nan_from_real_values(tmp_path: Path):
    """真实 legacy 库里 ssrdc 这种"数组被写过但全是 NaN"的情形，has_data 必须
    能识别——不能只看数组存不存在。"""
    ensure_store(tmp_path, "202601")
    assert has_data(tmp_path, "202601", "ssrdc") is False   # 骨架：全 NaN

    all_nan = np.full((31 * 24, 161, 281), np.nan, dtype="float32")
    write_variable(tmp_path, "202601", "ssrdc", all_nan, {})
    assert has_data(tmp_path, "202601", "ssrdc") is False   # 写过，但仍全 NaN

    real = np.full((31 * 24, 161, 281), 1.0, dtype="float32")
    write_variable(tmp_path, "202601", "t2m", real, {})
    assert has_data(tmp_path, "202601", "t2m") is True


def test_has_data_false_for_missing_month_or_variable(tmp_path: Path):
    assert has_data(tmp_path, "209901", "t2m") is False   # 月库不存在
    ensure_store(tmp_path, "202601")
    assert has_data(tmp_path, "202601", "not_a_real_var") is False


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
