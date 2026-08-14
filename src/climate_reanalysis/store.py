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

from climate_reanalysis.logging_setup import get_logger
from climate_reanalysis.period import month_hours
from climate_reanalysis.variables import VAR_SPECS

log = get_logger(__name__)

_LATTICE = 0.25
_TOL = 1e-6

#: 全管线统一中国框，与 ifs-hres 逐格相同——不得由数据推导
TARGET_LAT: np.ndarray = np.round(np.arange(15.0, 55.0 + _TOL, _LATTICE), 6)
TARGET_LON: np.ndarray = np.round(np.arange(70.0, 140.0 + _TOL, _LATTICE), 6)

SHARD = 32      # 落盘文件粒度
CHUNK = 8       # 读取粒度（zarr v3 sharding）

_PROV_KEY = "zrread_provenance"

_SKELETON_COORDS = ("time", "latitude", "longitude")


class StoreCorruptedError(RuntimeError):
    """月库无法打开——磁盘级损坏，不是「骨架未建完」。

    绝不能在这种情况下自动重建：库里可能还有幸存的真实数据/溯源，``mode="w"``
    会把它们连同损坏的部分一起清空，而且不出任何异常、不留任何日志。必须
    人工介入（检查/删除该月库后重新 download + restore）。
    """


class IncompleteStoreHasDataError(RuntimeError):
    """月库骨架不完整（缺 consolidated 元数据或坐标/变量不全），但库里已经有
    真实数据或溯源——多半是建骨架/写入中途崩溃留下的半成品，而不是从未使用
    过的空壳。

    自动重建会静默清空这些已有内容，所以拒绝重建、抛错点名，交给人工核实
    「哪些变量已经有数据」之后再决定是重建还是修复。
    """


def store_path(store_dir: Path, month: str) -> Path:
    """月库落盘位置——按年分层：``<store_dir>/<YYYY>/<YYYYMM>_era5.zarr``。

    年份直接取月份 token 的前四位，不另外解析——``month`` 全项目统一是
    ``YYYYMM`` 六位字符串，这个前提在 ``variables.py``/``period.py`` 等处也
    是硬编码假设。持续积累管线每年新增 12 个库，按年分层是运维（rsync/校验/
    归档按年切）的最小代价改动；此前的扁平 240 个库由独立的一次性迁移脚本
    （``scripts/migrate_to_year_layout.py``）挪，不在这里做兼容判断——本项目
    是这份档案的唯一生产者，不需要像 zrread 读取端那样兼容旧布局。
    """
    year = month[:4]
    return Path(store_dir) / year / f"{month}_era5.zarr"


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


def _required_members() -> set[str]:
    return set(_SKELETON_COORDS) | set(VAR_SPECS)


def _open_existing_or_raise(path: Path, month: str, mode: str = "r+"):
    """打开一个已确认存在（``path.exists()``）的月库。打不开视为磁盘级损坏——
    不是「骨架未建完」，绝不能吞掉异常后当「不存在」处理（那会让调用方掉进
    ``mode="w"`` 重建分支，把损坏库上任何幸存内容静默清空，见
    ``StoreCorruptedError`` 的说明）。
    """
    try:
        return zarr.open_group(str(path), mode=mode)
    except Exception as exc:
        raise StoreCorruptedError(
            f"{month} 月库无法打开：{path}（{exc!r}）——这是磁盘级损坏，不是"
            "「未建完」，不会自动重建；需要人工检查/删除该月库、必要时重新 "
            "download + restore"
        ) from exc


def _has_real_content(g) -> bool:
    """已打开的 group 里是否存在任何真实数据或溯源——骨架不完整时用来判断能不能
    安全重建，任意一项为真都不许重建（会清空）。

    数据数组要真的读一次值判断（不能只看数组存不存在），理由同 ``has_data``：
    真实 legacy 库的 ``ssrdc`` 数组确实被建过，只是从未写入真实数值，结构上跟
    "有数据"没有区别。任何一个数组读取失败也保守地当作"有内容"，不放行自动
    重建（读取失败本身就说明这不是一个安全可以覆盖的空壳）。
    """
    if dict(g.attrs).get(_PROV_KEY):
        return True
    for name in g.array_keys():
        if name in _SKELETON_COORDS:
            continue
        try:
            if not bool(np.isnan(g[name][:]).all()):
                return True
        except Exception:
            return True
    return False


def _build_skeleton(path: Path, month: str):
    """新建/重建全 NaN 骨架（``mode="w"`` 整体覆盖），返回打开的、可写的 group
    句柄。调用方须自行确保这一步是安全的（路径不存在，或已确认是空壳）——本
    函数不做这个判断，只管建。

    未写入的 chunk 零内存零磁盘——完整一月骨架实测仅 0.03 MB（逻辑 0.67 GB）。
    """
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
    return g


def _ensure_store_group(store_dir: Path, month: str):
    """确保月库骨架完整，返回一个已打开、可写（``mode="r+"``）的 group 句柄——
    ``write_variable`` 直接复用这个句柄写入，不必在确认完整之后再重新 open
    一次（避免对已完整库的重复 I/O）。

    四种情况，分开处理，不合并（合并就是本条 Critical 修复之前的 bug）：

    1. 路径不存在——新建骨架，没有东西可丢，``mode="w"`` 安全。
    2. 能打开、骨架齐全（consolidated 元数据存在且覆盖全部契约坐标与变量）——
       直接返回这个句柄。
    3. 能打开、骨架不齐（建到一半崩溃）：
       - 已经有真实数据或溯源——**不许清空**，抛 ``IncompleteStoreHasDataError``
         点名月份、路径，交给人工决定。
       - 确认是空壳（无数据无溯源）——安全重建，打日志说明原因。
    4. 打不开／打开时报错（损坏）——``_open_existing_or_raise`` 直接抛
       ``StoreCorruptedError``，绝不重建。
    """
    path = store_path(store_dir, month)
    if not path.exists():
        return _build_skeleton(path, month)

    g = _open_existing_or_raise(path, month, mode="r+")
    consolidated = g.metadata.consolidated_metadata
    complete = consolidated is not None and _required_members().issubset(
        set(consolidated.metadata.keys())
    )
    if complete:
        return g
    if _has_real_content(g):
        raise IncompleteStoreHasDataError(
            f"{month} 月库骨架不完整（{path}），但已经检测到真实数据或溯源——"
            "为避免静默清空，拒绝自动重建；请人工核实该月已有哪些变量的数据"
            "后再决定重建还是修复"
        )
    log.warning(
        "rebuilding_empty_skeleton", month=month, path=str(path),
        reason="骨架不完整（缺 consolidated 元数据或坐标/变量不全），但确认"
              "是空壳（无数据无溯源）——安全重建",
    )
    return _build_skeleton(path, month)


def ensure_store(store_dir: Path, month: str) -> Path:
    """月库骨架不完整（含不存在）则按契约新建/重建（全 NaN 骨架）；已完整则
    原样返回；骨架不齐但已有真实数据、或库已损坏打不开，则拒绝重建、抛错点名
    （四种情况的完整说明见 ``_ensure_store_group``）。

    对外只返回 ``Path``——沿用既有契约，测试与 migrate/cli 都按这个签名用。
    """
    _ensure_store_group(store_dir, month)
    return store_path(store_dir, month)


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

    直接走 ``_ensure_store_group`` 拿一个已确认完整、已打开的可写句柄——不再
    像 ``ensure_store`` 那样只拿一个 ``Path`` 再重新 open 一次（对已完整库是
    纯重复 I/O：完整性判据本身就需要 open 一次读 consolidated 元数据）。
    """
    g = _ensure_store_group(store_dir, month)
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
