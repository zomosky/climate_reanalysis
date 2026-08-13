"""月度国网库：产出契约与溯源属性。

产出契约由 zrread 的 era5 源固定（spec §2.1），**不得改动**：网格与 ifs-hres 逐格
相同，故 zrread 的掩膜 fraction 与 wmean 权重缓存直接复用。

**溯源属性是幂等的载体**（spec §4）：``zrread_provenance[产出变量]`` 记下产出它的
manifest 的 sha256。restore 逐变量比对——状态就在产出里，产出被删/被改，状态自然
跟着变；外部账本才会与现实脱节。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import zarr

from climate_reanalysis.period import month_hours
from climate_reanalysis.variables import VAR_SPECS

_LATTICE = 0.25
_TOL = 1e-6

#: 全管线统一中国框，与 ifs-hres 逐格相同——不得由数据推导
TARGET_LAT: np.ndarray = np.round(np.arange(15.0, 55.0 + _TOL, _LATTICE), 6)
TARGET_LON: np.ndarray = np.round(np.arange(70.0, 140.0 + _TOL, _LATTICE), 6)

SHARD = 32      # 落盘文件粒度
CHUNK = 8       # 读取粒度（zarr v3 sharding）

_PROV_KEY = "zrread_provenance"

_SKELETON_COORDS = ("time", "latitude", "longitude")


def store_path(store_dir: Path, month: str) -> Path:
    return Path(store_dir) / f"{month}_era5.zarr"


def check_area_matches_grid(area: tuple[float, float, float, float]) -> None:
    """``settings.area``（N, W, S, E）必须与本模块的契约网格
    ``TARGET_LAT``/``TARGET_LON`` 的首末格点吻合——这是两份独立的真值，没有
    互相校验就会各自漂移。

    改错 area 不会在下载时报错：CDS 老老实实按错的框给数据，下载本身完全
    "成功"。要等到整月下载完、进 restore 时 ``load_raw`` 逐文件核对网格才会
    报"网格不符"——代价是已经白白花掉的 CDS 排队配额和数小时带宽。这个比较
    本身几乎零成本，宁可在提交任何真实请求之前的启动期就把配置错误拦下来。
    """
    n, w, s, e = area
    expect = (float(TARGET_LAT[-1]), float(TARGET_LON[0]),
             float(TARGET_LAT[0]), float(TARGET_LON[-1]))
    got = (float(n), float(w), float(s), float(e))
    if any(abs(a - b) > _TOL for a, b in zip(got, expect)):
        en, ew, es, ee = expect
        raise ValueError(
            f"settings.area {area}（N,W,S,E）与契约网格不符：期望 "
            f"(N={en}, W={ew}, S={es}, E={ee})——area 与 store.TARGET_LAT/"
            f"TARGET_LON 是两份独立的真值，改一处必须同步改另一处"
        )


def _skeleton_is_complete(path: Path) -> bool:
    """完整性判据：consolidated 元数据存在，且骨架坐标与全部变量齐全。

    ``path.exists()``（目录在）不等于「骨架建完」——建骨架途中崩溃会在磁盘上
    留下一个只有部分数组、缺坐标的 group；若拿路径存在当判据，这个残缺库会被
    永久当成完整，之后任何一次 write_variable 都会在它之上写入数据、再把这个
    残缺状态用它*自己*的 consolidate 调用标记成「完整」——错误从此再也不会被
    发现。consolidated 元数据是本项目统一的完成标记（同 write_variable 的
    「写入顺序：数据 → 溯源 → consolidated 元数据」），故检查它是否存在、以及
    它记录的成员是否覆盖全部契约坐标与变量，而不是只看目录在不在。
    """
    if not path.exists():
        return False
    try:
        g = zarr.open_group(str(path), mode="r")
    except Exception:
        return False
    consolidated = g.metadata.consolidated_metadata
    if consolidated is None:
        return False
    members = set(consolidated.metadata.keys())
    required = set(_SKELETON_COORDS) | set(VAR_SPECS)
    return required.issubset(members)


def ensure_store(store_dir: Path, month: str) -> Path:
    """月库骨架不完整（含不存在）则按契约新建/重建（全 NaN 骨架）；已完整则原样返回。

    「重建」用 ``mode="w"`` 整体覆盖——会连带清掉崩溃留下的残缺内容，不会跟旧
    的部分数组混在一起产出更奇怪的状态。

    未写入的 chunk 零内存零磁盘——完整一月骨架实测仅 0.03 MB（逻辑 0.67 GB）。
    """
    path = store_path(store_dir, month)
    if _skeleton_is_complete(path):
        return path
    times = month_hours(month)
    n = times.size
    path.parent.mkdir(parents=True, exist_ok=True)
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
         {"units": "nanoseconds since 1970-01-01",
          "calendar": "proleptic_gregorian"}),
        ("latitude", TARGET_LAT, ("latitude",), {"units": "degrees_north"}),
        ("longitude", TARGET_LON, ("longitude",), {"units": "degrees_east"}),
    ):
        a = g.create_array(cname, shape=vals.shape, dtype=str(vals.dtype),
                           chunks=vals.shape, dimension_names=cdims,
                           attributes=attrs)
        a[:] = vals
    g.attrs.update({
        "zrread_source": "era5",
        "zrread_grid": (
            f"lat {TARGET_LAT[0]}..{TARGET_LAT[-1]} n={TARGET_LAT.size}; "
            f"lon {TARGET_LON[0]}..{TARGET_LON[-1]} n={TARGET_LON.size}; 0.25deg"
        ),
        "zrread_note": (
            "ERA5 原生整点轴：累积量为前一小时的区间平均通量（标签在右边界），"
            "其余为瞬时值。由 climate_reanalysis 生产。"
        ),
        _PROV_KEY: {},
    })
    zarr.consolidate_metadata(g.store)   # 完成标记最后写
    return path


def _normalize_provenance(raw) -> dict:
    """溯源属性的 str-or-dict 归一：zarr 的 attrs 视后端/序列化路径不同，取
    出来的嵌套值有时是 JSON 字符串、有时原样是 dict。此前 read_provenance/
    set_provenance/write_variable 三处各自重复这两行判断，容易三份各自漂移
    （改一处忘另外两处）——收敛成一份，谁都调这一个。
    """
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def read_provenance(store_dir: Path, month: str) -> dict:
    path = store_path(store_dir, month)
    if not path.exists():
        return {}
    g = zarr.open_group(str(path), mode="r")
    return _normalize_provenance(dict(g.attrs).get(_PROV_KEY, {}))


def has_data(store_dir: Path, month: str, var: str) -> bool:
    """该变量数组里是否存在任何非 NaN 值。

    用于区分"骨架里有这个变量的数组"和"这个变量真的有数据"——真实 legacy
    库里 ``ssrdc`` 就是前者不是后者：数组确实被写过，只是从未有真实数值填
    进去，全是 NaN。这种情况不能只看数组存不存在，或者只看
    ``nchunks_initialized`` 之类的结构信号（真实数据即使全 NaN 也已经写入过
    chunk，结构上看跟"有数据"没有区别）——必须真的读一次值。
    """
    path = store_path(store_dir, month)
    if not path.exists():
        return False
    g = zarr.open_group(str(path), mode="r")
    if var not in set(g.array_keys()):
        return False
    return not bool(np.isnan(g[var][:]).all())


def set_provenance(store_dir: Path, month: str, var: str, entry: dict) -> None:
    g = zarr.open_group(str(store_path(store_dir, month)), mode="r+")
    prov = _normalize_provenance(dict(g.attrs).get(_PROV_KEY, {}))
    prov[var] = entry
    g.attrs[_PROV_KEY] = prov
    zarr.consolidate_metadata(g.store)


def write_variable(
    store_dir: Path, month: str, var: str, data: np.ndarray, provenance: dict
) -> None:
    """写入单个变量并更新其溯源。其余变量一字不动。

    写入顺序：数据 → 溯源 → consolidated 元数据。最后一步是完成标记，故中途
    崩溃的库不会被误判为完整。溯源这一步复用 ``set_provenance``（而不是自己
    再读改写一遍 attrs）——三处各自维护同一段 str-or-dict 归一逻辑容易漂移，
    这里数据先落盘、再调用它，consolidate 仍然是最后发生的事。
    """
    path = ensure_store(store_dir, month)
    g = zarr.open_group(str(path), mode="r+")
    expect = (month_hours(month).size, TARGET_LAT.size, TARGET_LON.size)
    if tuple(data.shape) != expect:
        raise ValueError(f"{month}/{var}: 形状 {data.shape} != 契约 {expect}")
    if var not in set(g.array_keys()):
        spec = VAR_SPECS[var]
        g.create_array(
            var, shape=expect, dtype="float32",
            chunks=(expect[0], CHUNK, CHUNK), shards=(expect[0], SHARD, SHARD),
            fill_value=np.nan, dimension_names=("time", "latitude", "longitude"),
            attributes={"units": spec.units, "GRIB_stepType": spec.step_type},
        )
    g[var][:] = data.astype("float32", copy=False)
    set_provenance(store_dir, month, var, provenance)
