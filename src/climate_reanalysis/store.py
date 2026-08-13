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


def store_path(store_dir: Path, month: str) -> Path:
    return Path(store_dir) / f"{month}_era5.zarr"


def ensure_store(store_dir: Path, month: str) -> Path:
    """月库不存在则按契约新建（全 NaN 骨架）；已存在则原样返回。

    未写入的 chunk 零内存零磁盘——完整一月骨架实测仅 0.03 MB（逻辑 0.67 GB）。
    """
    path = store_path(store_dir, month)
    if path.exists():
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


def read_provenance(store_dir: Path, month: str) -> dict:
    path = store_path(store_dir, month)
    if not path.exists():
        return {}
    g = zarr.open_group(str(path), mode="r")
    prov = dict(g.attrs).get(_PROV_KEY, {})
    return json.loads(prov) if isinstance(prov, str) else dict(prov)


def set_provenance(store_dir: Path, month: str, var: str, entry: dict) -> None:
    g = zarr.open_group(str(store_path(store_dir, month)), mode="r+")
    prov = dict(g.attrs).get(_PROV_KEY, {})
    prov = json.loads(prov) if isinstance(prov, str) else dict(prov)
    prov[var] = entry
    g.attrs[_PROV_KEY] = prov
    zarr.consolidate_metadata(g.store)


def write_variable(
    store_dir: Path, month: str, var: str, data: np.ndarray, provenance: dict
) -> None:
    """写入单个变量并更新其溯源。其余变量一字不动。

    写入顺序：数据 → 溯源 → consolidated 元数据。最后一步是完成标记，故中途
    崩溃的库不会被误判为完整。
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
    prov = dict(g.attrs).get(_PROV_KEY, {})
    prov = json.loads(prov) if isinstance(prov, str) else dict(prov)
    prov[var] = provenance
    g.attrs[_PROV_KEY] = prov
    zarr.consolidate_metadata(g.store)
