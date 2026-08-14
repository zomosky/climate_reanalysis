"""省库 → 月度国网库的烘焙。

遍历顺序：**按输出分片 × 变量，一次读完该分片的全时段，再切月写出**。

省库分块是 ``(175320, 4, 4)``——整条时间轴一个块，读任一个月都要解压 20 年。
若按月遍历，240 个月就是 240 倍读放大；按分片遍历则每个源块只读一次。分片同时
是原子写入单位，整块写出无读-改-写，且各分片互不相交、可并行。

内存峰值 = 分片尺寸决定：20 年 × 32×32 × float32 ≈ 718 MB。
源 tile 必须**惰性切片**读（``zarr_array[t0:t1, r0:r1, c0:c1]``），整省
``np.asarray`` 会把 4.2 GB 拉进内存。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import xarray as xr

from climate_reanalysis.legacy_bake.deconv import from_interval_centre
from climate_reanalysis.legacy_bake.grid import (
    CHUNK,
    SHARD,
    TARGET_LAT,
    TARGET_LON,
    Ledger,
    coverage_from_tiles,
    shard_slices,
    tile_coverage,
    tiles_fingerprint,
)
from climate_reanalysis.store import store_path

#: 省库时间戳 = ERA5 原生整点 − 30min（solarcast 为 pvlib 做的中点化）
CENTRE_OFFSET = np.timedelta64(30, "m")


@dataclass(frozen=True)
class VarSpec:
    """目标变量的来历：从哪些源变量算、怎么算、单位与 stepType。"""

    sources: tuple[str, ...]
    transform: Callable[..., np.ndarray]
    units: str
    step_type: str
    #: 变换是否依赖序列历史。中点化反解 x[i]=2y[i]-x[i-1] 的播种条件是
    #: y[0]==x[0]（正演的边界保持），只在**整条序列的原点**成立；从切片
    #: 起点播种等于把一个中点均值当瞬时值，而齐次解 (-1)^i·C 不衰减，
    #: 误差会以交替符号贯穿整段（实测烘 2 月时 48 个值全错、最大 0.66 K）。
    needs_history: bool = False


def _identity(a: np.ndarray) -> np.ndarray:
    return a.astype("float64")


def _bhi(ghi: np.ndarray, dhi: np.ndarray) -> np.ndarray:
    """水平面直射 = 总辐射 − 散射（省库存的是 dhi = ghi − bhi，精确可逆）。"""
    return np.clip(ghi.astype("float64") - dhi.astype("float64"), 0.0, None)


def _deconv_to_kelvin(a: np.ndarray) -> np.ndarray:
    return from_interval_centre(a, axis=0) + 273.15


def _deconv(a: np.ndarray) -> np.ndarray:
    return from_interval_centre(a, axis=0)


#: 通量只重贴标签（无损）；瞬时量须反解中点化——中点标记会让 kt 分母窗口
#: 错位 30min（晨昏误差实测 20-80%）。见 spec §4.3。
VAR_SPECS: dict[str, VarSpec] = {
    "ssrd": VarSpec(("ghi",), _identity, "W m**-2", "avg"),
    "fdir": VarSpec(("ghi", "dhi"), _bhi, "W m**-2", "avg"),
    "t2m": VarSpec(("temp_air",), _deconv_to_kelvin, "K", "instant",
                   needs_history=True),
    "si10": VarSpec(("wind_speed",), _deconv, "m s**-1", "instant",
                    needs_history=True),
    "sp": VarSpec(("pressure",), _deconv, "Pa", "instant", needs_history=True),
    # 第二批（2026-08 追加）。省库里这些已是 ERA5 短名 + SI，故无单位换算；
    # 瞬时量同样被中点化过，须反解；ssrdc 是区间平均通量，仅重贴标签。
    "u100": VarSpec(("u100",), _deconv, "m s**-1", "instant", needs_history=True),
    "v100": VarSpec(("v100",), _deconv, "m s**-1", "instant", needs_history=True),
    "d2m": VarSpec(("d2m",), _deconv, "K", "instant", needs_history=True),
    "ssrdc": VarSpec(("ssrdc",), _identity, "W m**-2", "avg"),
}


def month_hours(month: str, hours: int | None = None) -> np.ndarray:
    """该月的原生整点 UTC 时间轴（``hours`` 仅供测试截短）。"""
    y, m = int(month[:4]), int(month[4:6])
    start = np.datetime64(f"{y:04d}-{m:02d}-01T00:00", "ns")
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    n = int(
        (np.datetime64(f"{ny:04d}-{nm:02d}-01T00:00", "ns") - start)
        / np.timedelta64(1, "h")
    )
    if hours is not None:
        n = min(n, hours)
    return start + np.arange(n) * np.timedelta64(1, "h")


@dataclass
class Tile:
    """省库 tile 的惰性句柄——数组不载入，按需切片。"""

    path: Path
    arrays: dict
    lat: np.ndarray
    lon: np.ndarray
    times: np.ndarray  # 已加回 30min，即 ERA5 原生整点


def open_tile(path: Path) -> Tile:
    """打开省库 tile（惰性）。

    省库缺 ``dimension_names``，xarray 直接读会 KeyError，故走 zarr 原生句柄；
    维序由省库契约固定为 ``(time, lat, lon)``。
    """
    import zarr

    g = zarr.open_group(str(path), mode="r")
    skip = {"time", "lat", "lon", "elevation"}
    return Tile(
        path=Path(path),
        arrays={k: g[k] for k in g.array_keys() if k not in skip},
        lat=np.asarray(g["lat"][:], dtype="float64"),
        lon=np.asarray(g["lon"][:], dtype="float64"),
        times=_decode_times(g["time"]) + CENTRE_OFFSET,
    )


def _decode_times(arr) -> np.ndarray:
    """省库的 time 是裸 int64 纳秒；但任何经 xarray 写出的库会带 CF ``units``
    编码（存的是「since epoch 的小时/秒数」而非纳秒），直接 astype 会得到
    1970 年的垃圾值。两种都要认。"""
    raw = np.asarray(arr[:])
    units = arr.attrs.get("units")
    if units:
        from xarray.coding.times import decode_cf_datetime

        decoded = decode_cf_datetime(raw, units, arr.attrs.get("calendar"))
        return np.asarray(decoded).astype("datetime64[ns]")
    return raw.astype("datetime64[ns]")


def create_month_store(
    root: Path,
    month: str,
    hours: int | None = None,
    coverage: np.ndarray | None = None,
) -> Path:
    """建一个全 NaN 的空月库（分片/分块/consolidated 就位）。

    未写入的 chunk 不占空间，故稠密逻辑网格的落盘量约等于实际数据量。
    ``coverage`` 作为**坐标**写入（不是数据变量）——它带 lat/lon 维，若做成数据
    变量会进入源词表、被变量投影当成可查变量；做成坐标则随数据集自然携带。
    """
    times = month_hours(month, hours)
    n = times.size
    root = Path(root)
    path = store_path(root, month)
    path.parent.mkdir(parents=True, exist_ok=True)
    if coverage is None:
        coverage = np.ones((TARGET_LAT.size, TARGET_LON.size), dtype=bool)

    import zarr

    # 用 zarr 原生建骨架而非 xarray：后者要先物化 (744,161,281)×5 变量的 NaN
    # 数组（673 MB 常驻，会和 718 MB 的分片块叠加），且把未覆盖区也写成实体
    # chunk。原生建法下 fill_value=NaN 的未写入 chunk 零内存、零磁盘、零耗时。
    g = zarr.open_group(str(path), mode="w")
    dims = ("time", "latitude", "longitude")
    for name, spec in VAR_SPECS.items():
        g.create_array(
            name,
            shape=(n, TARGET_LAT.size, TARGET_LON.size),
            dtype="float32",
            chunks=(n, CHUNK, CHUNK),
            shards=(n, SHARD, SHARD),
            fill_value=np.nan,
            dimension_names=dims,
            attributes={"units": spec.units, "GRIB_stepType": spec.step_type},
        )
    for cname, vals, cdims, attrs in (
        ("time", times.astype("int64"), ("time",),
         {"units": "nanoseconds since 1970-01-01", "calendar": "proleptic_gregorian"}),
        ("latitude", TARGET_LAT, ("latitude",), {"units": "degrees_north"}),
        ("longitude", TARGET_LON, ("longitude",), {"units": "degrees_east"}),
    ):
        a = g.create_array(cname, shape=vals.shape, dtype=str(vals.dtype),
                           chunks=vals.shape, dimension_names=cdims,
                           attributes=attrs)
        a[:] = vals
    cov = g.create_array(
        "coverage", shape=coverage.shape, dtype="uint8",
        chunks=coverage.shape, dimension_names=("latitude", "longitude"),
        attributes={"long_name": "该格点是否有源数据（0 = 覆盖范围外，恒为 NaN）"},
    )
    cov[:] = coverage.astype("uint8")

    g.attrs.update({
        "zrread_source": "era5",
        "coordinates": "coverage",  # 让 xarray 把它当坐标而非数据变量
        "zrread_grid": (
            f"lat {TARGET_LAT[0]}..{TARGET_LAT[-1]} n={TARGET_LAT.size}; "
            f"lon {TARGET_LON[0]}..{TARGET_LON[-1]} n={TARGET_LON.size}; 0.25deg"
        ),
        "zrread_note": (
            "ERA5 原生整点轴：ssrd/fdir 为前一小时的区间平均通量（标签在右边界），"
            "其余为瞬时值。t2m/si10/sp 由 solarcast 中点化反解还原，残差 ~0.015K。"
        ),
    })
    zarr.consolidate_metadata(g.store)
    return path


def _assemble_shard(
    tiles: list[Tile], name: str, la: slice, lo: slice, all_times: np.ndarray
) -> np.ndarray | None:
    """拼装一个分片在全时段上的数据；无任何 tile 覆盖时返回 None。

    重叠格点来自同一份 ERA5 的同一格点，值必须逐位一致——不一致抛错，绝不
    静默让后写的覆盖先写的。
    """
    spec = VAR_SPECS[name]
    block = np.full((all_times.size, la.stop - la.start, lo.stop - lo.start),
                    np.nan, dtype="float32")
    touched = False
    for tile in tiles:
        hit = tile_coverage(tile.lat, tile.lon)
        if hit is None:
            continue
        tla, tlo, rows, cols = hit
        i0, i1 = max(tla.start, la.start), min(tla.stop, la.stop)
        j0, j1 = max(tlo.start, lo.start), min(tlo.stop, lo.stop)
        if i0 >= i1 or j0 >= j1:
            continue
        if any(s not in tile.arrays for s in spec.sources):
            continue  # 该 tile 还没有这个变量（增量补下中）

        # 时间：两侧都是单调整点，取连续交集（惰性切片，勿用花式索引）
        t_lo = max(tile.times[0], all_times[0])
        t_hi = min(tile.times[-1], all_times[-1])
        if t_lo > t_hi:
            continue
        s0 = int(np.searchsorted(tile.times, t_lo))
        s1 = int(np.searchsorted(tile.times, t_hi)) + 1

        r0 = int(rows[i0 - tla.start])
        r1 = int(rows[i1 - 1 - tla.start]) + 1
        c0 = int(cols[j0 - tlo.start])
        c1 = int(cols[j1 - 1 - tlo.start]) + 1
        # 反解类变换必须从序列原点起算（见 VarSpec.needs_history）。源库的
        # chunk 本就是整条时间轴，多读的是已解压的同一块，无额外 I/O 代价。
        read0 = 0 if spec.needs_history else s0
        raw = [tile.arrays[s][read0:s1, r0:r1, c0:c1] for s in spec.sources]
        vals = spec.transform(*raw)[s0 - read0 :].astype("float32")

        # 源读取是连续切片（惰性要求），但 all_times 未必连续——烘不相邻的
        # 月份时它是几段的并集。故写入位置按 searchsorted 显式映射，只落在
        # 确实属于目标轴的那些时刻上。
        src_t = tile.times[s0:s1]
        pos = np.searchsorted(all_times, src_t)
        hit = (pos < all_times.size) & (
            all_times[np.minimum(pos, all_times.size - 1)] == src_t
        )
        if not hit.any():
            continue
        pos, vals = pos[hit], vals[hit]

        si = slice(i0 - la.start, i1 - la.start)
        sj = slice(j0 - lo.start, j1 - lo.start)
        cur = block[pos, si, sj]
        both = ~np.isnan(cur) & ~np.isnan(vals)
        if both.any() and not np.allclose(
            cur[both], vals[both], rtol=0, atol=1e-4
        ):
            raise ValueError(
                f"{name}: 省际重叠格点值不一致（{tile.path.name}）——同一份 ERA5 "
                "的同一格点不应出现分歧，请先核对源 tile"
            )
        block[pos, si, sj] = vals
        touched = True
    return block if touched else None


def bake(
    tiles: list[Path],
    out_root: Path,
    months: list[str],
    ledger: Ledger,
    variables: list[str] | None = None,
    hours: int | None = None,
) -> None:
    """把一批省库 tile 烘进月度国网库。

    ``variables=None`` 表示全部；指定则只跑那几个（增量加变量的入口）。
    """
    names = list(variables or VAR_SPECS)
    out_root = Path(out_root)
    loaded = [open_tile(Path(p)) for p in tiles]
    # tile 集合变了就作废账本——否则补下的新 tile 会被整片跳过且不报错
    fp = tiles_fingerprint(tiles)
    if ledger.tiles_fingerprint is None:
        ledger.tiles_fingerprint = fp
    elif ledger.tiles_fingerprint != fp:
        ledger._done.clear()
        ledger.tiles_fingerprint = fp
    cov = coverage_from_tiles([(t.lat, t.lon) for t in loaded])

    per_month = {m: month_hours(m, hours) for m in months}
    for month, times in per_month.items():
        path = store_path(out_root, month)
        if not path.exists():
            create_month_store(out_root, month, hours, cov)
        else:
            # 已有库也要刷新 coverage：补下新 tile 后它会少报，消费端会误以为
            # 那里没数据。数组很小（161×281 uint8 ≈ 45 KB），无脑重写即可。
            # 同时补建缺失的变量数组——VAR_SPECS 增补了新变量（u100/v100/d2m/
            # ssrdc）时，已有月库不会自动长出这些数组，重烘会静默漏写。
            import zarr

            g = zarr.open_group(str(path), mode="r+")
            g["coverage"][:] = cov.astype("uint8")
            have = set(g.array_keys())
            nt = g["time"].shape[0]
            for vn in names:
                if vn in have:
                    continue
                spec = VAR_SPECS[vn]
                g.create_array(
                    vn, shape=(nt, TARGET_LAT.size, TARGET_LON.size),
                    dtype="float32", chunks=(nt, CHUNK, CHUNK),
                    shards=(nt, SHARD, SHARD), fill_value=np.nan,
                    dimension_names=("time", "latitude", "longitude"),
                    attributes={"units": spec.units,
                                "GRIB_stepType": spec.step_type},
                )
            zarr.consolidate_metadata(g.store)
    all_times = np.unique(np.concatenate(list(per_month.values())))

    for shard_id, (la, lo) in enumerate(shard_slices()):
        for name in names:
            todo = [m for m in months if not ledger.done(month=m, var=name,
                                                         shard_id=shard_id)]
            if not todo:
                continue
            block = _assemble_shard(loaded, name, la, lo, all_times)
            if block is not None:
                for month in todo:
                    times = per_month[month]
                    d0 = int(np.searchsorted(all_times, times[0]))
                    part = xr.Dataset(
                        {name: (("time", "latitude", "longitude"),
                                block[d0:d0 + times.size])},
                        coords={"time": times,
                                "latitude": TARGET_LAT[la],
                                "longitude": TARGET_LON[lo]},
                    )
                    part.to_zarr(
                        store_path(out_root, month),
                        mode="r+",
                        region={"time": slice(0, times.size),
                                "latitude": la, "longitude": lo},
                    )
            for month in todo:
                ledger.mark(month, name, shard_id)
        ledger.save()
