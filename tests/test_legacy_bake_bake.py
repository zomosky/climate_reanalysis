"""烘焙：合成 tile → 月度国网库。断言落格、单位、时间轴、重叠一致、幂等。"""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from climate_reanalysis.legacy_bake.bake import CHUNK, SHARD, bake, create_month_store, open_tile
from climate_reanalysis.legacy_bake.grid import TARGET_LAT, TARGET_LON, Ledger
from climate_reanalysis.store import store_path

HOURS = 48  # 两天，够覆盖跨日且跑得快
START = np.datetime64("2026-01-01T00:00", "ns")  # 原生整点轴的起点


def _make_tile(path, lat0, lat1, lon0, lon1, seed=0):
    """合成省库 tile：中点标记（:30）、pvlib 命名与单位，同真实省库。

    返回 (源数据集, 原生整点上的真实 temp_air)，后者用于断言反解正确。
    """
    lat = np.round(np.arange(lat0, lat1 + 1e-6, 0.25), 6)
    lon = np.round(np.arange(lon0, lon1 + 1e-6, 0.25), 6)
    t = START - np.timedelta64(30, "m") + np.arange(HOURS) * np.timedelta64(1, "h")
    rng = np.random.default_rng(seed)
    shape = (HOURS, lat.size, lon.size)

    # 由「原生整点值」正演出中点化的存储值，使反解期望精确可算
    x_temp = rng.normal(15.0, 5.0, shape).astype(np.float32)  # °C
    y_temp = x_temp.copy()
    y_temp[1:] = np.float32(0.5) * (x_temp[:-1] + x_temp[1:])

    ghi = np.abs(rng.normal(300.0, 100.0, shape)).astype(np.float32)
    data = {
        "ghi": ghi,
        "dhi": ghi * np.float32(0.3),
        "temp_air": y_temp,
        "wind_speed": np.full(shape, 4.0, np.float32),
        "pressure": np.full(shape, 101325.0, np.float32),
    }
    # 用裸 zarr 写，忠实模拟真实省库：zarr v3、无 dimension_names、
    # time 为裸 int64 纳秒（非 CF 编码）、整条时间轴一个 chunk
    import zarr

    g = zarr.open_group(str(path), mode="w")
    for k, v in data.items():
        a = g.create_array(k, shape=v.shape, dtype="float32",
                           chunks=(HOURS, 4, 4))
        a[:] = v
    for k, v in (("time", t.astype("datetime64[ns]").astype("int64")),
                 ("lat", lat), ("lon", lon)):
        a = g.create_array(k, shape=v.shape, dtype=str(v.dtype), chunks=v.shape)
        a[:] = v

    ds = xr.Dataset(
        {k: (("time", "lat", "lon"), v) for k, v in data.items()},
        coords={"time": t, "lat": lat, "lon": lon},
    )
    return ds, x_temp


def _ledger(tmp_path):
    return Ledger.load(tmp_path / "ledger.json")


def test_create_month_store_layout(tmp_path):
    p = create_month_store(tmp_path, "202601", HOURS)
    ds = xr.open_zarr(p, consolidated=True)
    assert ds.sizes["latitude"] == TARGET_LAT.size
    assert ds.sizes["longitude"] == TARGET_LON.size
    enc = ds["t2m"].encoding
    assert tuple(enc["chunks"]) == (HOURS, CHUNK, CHUNK)
    assert tuple(enc["shards"]) == (HOURS, SHARD, SHARD)
    assert np.isnan(ds["t2m"].values).all()  # 未写入即 NaN


def test_open_tile_is_lazy(tmp_path):
    """源 tile 必须惰性——整省 asarray 会把 4.2 GB 拉进内存。"""
    tile = tmp_path / "A.zarr"
    _make_tile(tile, 30.0, 31.0, 110.0, 111.0)
    t = open_tile(tile)
    assert not isinstance(t.arrays["ghi"], np.ndarray)
    assert t.arrays["ghi"].shape[0] == HOURS
    assert t.times[0] == START  # 已加回 30min


def test_coverage_coord_marks_only_tiled_cells(tmp_path):
    tile = tmp_path / "tiles" / "A.zarr"
    tile.parent.mkdir(parents=True)
    _make_tile(tile, 30.0, 31.0, 110.0, 111.0)
    out = tmp_path / "monthly"
    bake([tile], out, ["202601"], _ledger(tmp_path), hours=HOURS)

    ds = xr.open_zarr(store_path(out, "202601"), consolidated=True)
    assert "coverage" in ds.coords  # 坐标而非数据变量
    assert "coverage" not in ds.data_vars
    assert int(ds["coverage"].sel(latitude=30.0, longitude=110.0)) == 1
    assert int(ds["coverage"].sel(latitude=50.0, longitude=130.0)) == 0


def test_bake_lands_values_on_correct_cells_with_native_hour_axis(tmp_path):
    tile = tmp_path / "tiles" / "A.zarr"
    tile.parent.mkdir(parents=True)
    src, x_temp = _make_tile(tile, 30.0, 31.0, 110.0, 111.0)

    out = tmp_path / "monthly"
    bake([tile], out, ["202601"], _ledger(tmp_path), hours=HOURS)
    ds = xr.open_zarr(store_path(out, "202601"), consolidated=True)

    assert ds["time"].values[0] == START  # 时间轴还原到整点
    # 通量仅重贴标签：值与源库逐位相同
    np.testing.assert_array_equal(
        ds["ssrd"].sel(latitude=30.0, longitude=110.0).values,
        src["ghi"].isel(lat=0, lon=0).values,
    )
    # fdir = ghi - dhi
    np.testing.assert_allclose(
        ds["fdir"].sel(latitude=30.0, longitude=110.0).values,
        (src["ghi"] - src["dhi"]).isel(lat=0, lon=0).values,
        rtol=1e-6,
    )
    # t2m：反解 + 转 K
    np.testing.assert_allclose(
        ds["t2m"].sel(latitude=30.0, longitude=110.0).values,
        x_temp[:, 0, 0] + 273.15,
        atol=0.02,
    )
    # 框外与未覆盖处仍是 NaN
    assert np.isnan(ds["t2m"].sel(latitude=50.0, longitude=130.0).values).all()


def test_bake_clips_cells_below_15n(tmp_path):
    tile = tmp_path / "tiles" / "S.zarr"
    tile.parent.mkdir(parents=True)
    _make_tile(tile, 13.0, 16.0, 110.0, 110.5)
    out = tmp_path / "monthly"
    bake([tile], out, ["202601"], _ledger(tmp_path), hours=HOURS)
    ds = xr.open_zarr(store_path(out, "202601"), consolidated=True)
    assert not np.isnan(ds["t2m"].sel(latitude=15.0, longitude=110.0).values).any()
    assert float(ds["latitude"].min()) == 15.0  # 13-14.75 根本不在网格上


def test_bake_raises_on_inconsistent_overlap(tmp_path):
    """省际重叠格点来自同一份 ERA5 同一格，值必须一致——不一致要响亮失败。"""
    d = tmp_path / "tiles"
    d.mkdir()
    _make_tile(d / "A.zarr", 30.0, 31.0, 110.0, 111.0, seed=1)
    _make_tile(d / "B.zarr", 30.5, 31.5, 110.5, 111.5, seed=2)  # 不同 seed -> 冲突
    with pytest.raises(ValueError, match="重叠"):
        bake([d / "A.zarr", d / "B.zarr"], tmp_path / "monthly", ["202601"],
             _ledger(tmp_path), hours=HOURS)


def test_bake_accepts_consistent_overlap(tmp_path):
    """同值重叠必须放行——真实省库的 buffer 重叠就是这种情况。"""
    d = tmp_path / "tiles"
    d.mkdir()
    _make_tile(d / "A.zarr", 30.0, 31.0, 110.0, 111.0, seed=7)
    _make_tile(d / "B.zarr", 30.0, 31.0, 110.0, 111.0, seed=7)  # 同 seed -> 同值
    bake([d / "A.zarr", d / "B.zarr"], tmp_path / "monthly", ["202601"],
         _ledger(tmp_path), hours=HOURS)


def test_bake_is_idempotent_via_ledger(tmp_path):
    tile = tmp_path / "tiles" / "A.zarr"
    tile.parent.mkdir(parents=True)
    _make_tile(tile, 30.0, 31.0, 110.0, 111.0)
    out = tmp_path / "monthly"
    led = _ledger(tmp_path)

    bake([tile], out, ["202601"], led, hours=HOURS)
    first = xr.open_zarr(store_path(out, "202601"), consolidated=True)["t2m"].values.copy()
    bake([tile], out, ["202601"], led, hours=HOURS)  # 重跑
    second = xr.open_zarr(store_path(out, "202601"), consolidated=True)["t2m"].values
    np.testing.assert_array_equal(first, second)


def test_bake_single_variable_leaves_others_untouched(tmp_path):
    """增量加变量的入口：只跑指定变量，其余保持 NaN。"""
    tile = tmp_path / "tiles" / "A.zarr"
    tile.parent.mkdir(parents=True)
    _make_tile(tile, 30.0, 31.0, 110.0, 111.0)
    out = tmp_path / "monthly"
    bake([tile], out, ["202601"], _ledger(tmp_path), variables=["t2m"], hours=HOURS)
    ds = xr.open_zarr(store_path(out, "202601"), consolidated=True)
    assert not np.isnan(ds["t2m"].sel(latitude=30.0, longitude=110.0).values).any()
    assert np.isnan(ds["ssrd"].sel(latitude=30.0, longitude=110.0).values).all()


def test_deconv_seeded_from_series_origin_not_slice_start(tmp_path):
    """烘非首月时，反解必须从序列原点起算。

    from_interval_centre 假设 y[0]==x[0]（正演的边界保持），这只在整条序列的
    起点成立。若从切片起点 s0>0 处播种，等于把一个真正的中点均值当成瞬时值，
    而递推的齐次解 (-1)^i·C 不衰减——误差会以交替符号贯穿整月。
    """
    lat = np.round(np.arange(30.0, 30.51, 0.25), 6)
    lon = np.round(np.arange(110.0, 110.51, 0.25), 6)
    n = 31 * 24 + 48  # 2026-01 整月 + 2026-02 前两天
    t0 = np.datetime64("2026-01-01T00:00", "ns")
    t = t0 - np.timedelta64(30, "m") + np.arange(n) * np.timedelta64(1, "h")
    rng = np.random.default_rng(3)
    shape = (n, lat.size, lon.size)
    x = rng.normal(15.0, 5.0, shape).astype(np.float32)  # 原生整点真值 (°C)
    y = x.copy()
    y[1:] = np.float32(0.5) * (x[:-1] + x[1:])

    import zarr

    path = tmp_path / "T.zarr"
    g = zarr.open_group(str(path), mode="w")
    for k, v in (("temp_air", y), ("ghi", np.zeros(shape, np.float32)),
                 ("dhi", np.zeros(shape, np.float32)),
                 ("wind_speed", np.zeros(shape, np.float32)),
                 ("pressure", np.zeros(shape, np.float32))):
        a = g.create_array(k, shape=shape, dtype="float32", chunks=(n, 4, 4))
        a[:] = v
    for k, v in (("time", t.astype("int64")), ("lat", lat), ("lon", lon)):
        a = g.create_array(k, shape=v.shape, dtype=str(v.dtype), chunks=v.shape)
        a[:] = v

    out = tmp_path / "monthly"
    bake([path], out, ["202602"], _ledger(tmp_path), variables=["t2m"], hours=48)
    ds = xr.open_zarr(store_path(out, "202602"), consolidated=True)
    got = ds["t2m"].sel(latitude=30.0, longitude=110.0).values
    expect = x[31 * 24 : 31 * 24 + 48, 0, 0] + 273.15
    np.testing.assert_allclose(got, expect, atol=0.02)


def test_bake_handles_non_contiguous_months(tmp_path):
    """烘不相邻的月份：all_times 是几段的并集，写入位置不能假设连续。"""
    tile = tmp_path / "tiles" / "A.zarr"
    tile.parent.mkdir(parents=True)
    _make_tile(tile, 30.0, 31.0, 110.0, 111.0)
    out = tmp_path / "monthly"
    # tile 只覆盖 202601 前 48h；202603 无源数据，应保持全 NaN 而不是崩
    bake([tile], out, ["202601", "202603"], _ledger(tmp_path), hours=HOURS)
    a = xr.open_zarr(store_path(out, "202601"), consolidated=True)
    b = xr.open_zarr(store_path(out, "202603"), consolidated=True)
    assert not np.isnan(a["t2m"].sel(latitude=30.0, longitude=110.0).values).any()
    assert np.isnan(b["t2m"].sel(latitude=30.0, longitude=110.0).values).all()


def test_new_tile_invalidates_ledger(tmp_path):
    """补下新 tile 后重跑：账本必须作废，否则新数据不进库且不报错。"""
    d = tmp_path / "tiles"
    d.mkdir()
    _make_tile(d / "A.zarr", 30.0, 31.0, 110.0, 111.0)
    out = tmp_path / "monthly"
    lp = tmp_path / "ledger.json"

    from climate_reanalysis.legacy_bake.grid import tiles_fingerprint

    t1 = [d / "A.zarr"]
    bake(t1, out, ["202601"], Ledger.load(lp, tiles_fingerprint(t1)), hours=HOURS)
    ds = xr.open_zarr(store_path(out, "202601"), consolidated=True)
    assert np.isnan(ds["t2m"].sel(latitude=40.0, longitude=120.0).values).all()

    # 新增一个别处的 tile —— 若账本未作废，这片区域会保持 NaN
    _make_tile(d / "B.zarr", 40.0, 40.5, 120.0, 120.5, seed=9)
    t2 = [d / "A.zarr", d / "B.zarr"]
    led2 = Ledger.load(lp, tiles_fingerprint(t2))
    assert not led2.done("202601", "t2m", 0), "tile 集合变了，账本应作废"
    bake(t2, out, ["202601"], led2, hours=HOURS)
    ds = xr.open_zarr(store_path(out, "202601"), consolidated=True)
    assert not np.isnan(ds["t2m"].sel(latitude=40.0, longitude=120.0).values).any()
    assert int(ds["coverage"].sel(latitude=40.0, longitude=120.0)) == 1


def test_appended_tile_vars_flow_into_existing_month_store(tmp_path):
    """第二批变量落地路径：省库追加数组 → 重烘 → 已有月库长出对应数组并落数。

    两处 ensure 缺一不可：省库侧（multiyear.ensure_vars）与月库侧（bake 里对
    已存在的库补建缺失数组）。任一缺失都会静默漏写而不报错。
    """
    import zarr

    tile = tmp_path / "tiles" / "A.zarr"
    tile.parent.mkdir(parents=True)
    _make_tile(tile, 30.0, 31.0, 110.0, 111.0)
    out = tmp_path / "monthly"
    lp = tmp_path / "ledger.json"
    from climate_reanalysis.legacy_bake.grid import tiles_fingerprint

    bake([tile], out, ["202601"], Ledger.load(lp, tiles_fingerprint([tile])),
         hours=HOURS)
    ds = xr.open_zarr(store_path(out, "202601"), consolidated=True)
    assert np.isnan(ds["ssrdc"].values).all(), "源里还没有 ssrdc，应全 NaN"

    # 模拟补下：往省库追加 ssrdc（区间平均通量，不参与中点搬移）
    g = zarr.open_group(str(tile), mode="r+")
    ref = g["ghi"]
    a = g.create_array("ssrdc", shape=ref.shape, chunks=ref.chunks,
                       dtype="float32", fill_value=np.float32(np.nan))
    a[:] = np.asarray(ref[:]) * np.float32(1.25)  # 晴空 > 实际
    zarr.consolidate_metadata(g.store)

    led = Ledger.load(lp, tiles_fingerprint([tile]))
    assert not led.done("202601", "ssrdc", 0), "tile 内容变了，账本应作废"
    bake([tile], out, ["202601"], led, hours=HOURS)

    ds = xr.open_zarr(store_path(out, "202601"), consolidated=True)
    got = ds["ssrdc"].sel(latitude=30.0, longitude=110.0).values
    assert not np.isnan(got).any()
    np.testing.assert_allclose(
        got, ds["ssrd"].sel(latitude=30.0, longitude=110.0).values * 1.25, rtol=1e-5
    )
