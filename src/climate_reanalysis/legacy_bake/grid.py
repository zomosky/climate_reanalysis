"""目标网格（冻结）、源 tile 落位、烘焙账本。

网格取全管线统一的中国框：lat 15-55 / lon 70-140，0.25°，161x281——与
climate_restorage 落盘的 ifs-hres **逐格相同**。这不是巧合而是前提：zrread 的
掩膜 fraction 与 wmean 权重层按网格指纹缓存，同格即直接复用（该网格指纹
``aae4a29cfecd`` 下已有上百条缓存），跨源偏差订正也因此无需插值。

**不得由数据推导网格**——补下台湾与沿海缺口后要往同一个固定形状里填 region，
形状一变前面烘的全作废。故这里硬编码为常量，改动会直接触发本模块的冻结测试。

``TARGET_LAT``/``TARGET_LON``/``SHARD``/``CHUNK`` 从 ``climate_reanalysis.store``
import——那才是本项目的产出契约真值，这里不再重复定义一份（两份真值一旦漂移，
烘出来的网格会与 download/restore 那条路径产出的月库对不上、且不会有任何报错，
搬运时已用 ``np.array_equal`` 核对过两处历史取值逐位相同）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from climate_reanalysis.store import CHUNK, SHARD, TARGET_LAT, TARGET_LON

log = logging.getLogger(__name__)


def tiles_fingerprint(paths) -> str:
    """tile 集合的指纹：名字 + 各自 zarr 元数据的 mtime。

    名字变（新增/删除 tile）或内容变（追加了变量、重下了数据）都会改指纹。
    """
    parts = []
    for p in sorted(Path(x) for x in paths):
        meta = p / "zarr.json"
        stamp = meta.stat().st_mtime_ns if meta.exists() else 0
        parts.append(f"{p.name}:{stamp}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

#: 0.25° 点阵
_LATTICE = 0.25
_TOL = 1e-6


def shard_slices() -> list[tuple[slice, slice]]:
    """枚举全部输出分片在目标网格上的 (lat, lon) 切片，无重叠且铺满。"""
    out: list[tuple[slice, slice]] = []
    for i0 in range(0, TARGET_LAT.size, SHARD):
        for j0 in range(0, TARGET_LON.size, SHARD):
            out.append(
                (
                    slice(i0, min(i0 + SHARD, TARGET_LAT.size)),
                    slice(j0, min(j0 + SHARD, TARGET_LON.size)),
                )
            )
    return out


def _index_on_target(vals: np.ndarray, axis: np.ndarray, label: str):
    """把 tile 坐标映射到目标轴下标；返回 (目标下标, 采用的 tile 行号)。"""
    vals = np.round(np.asarray(vals, dtype="float64"), 6)
    off = np.round((vals - axis[0]) / _LATTICE)
    if np.any(np.abs(vals - (axis[0] + off * _LATTICE)) > _TOL):
        raise ValueError(
            f"{label} 坐标未落在 0.25° 点阵上——源 tile 数据有问题，不做静默吸附"
        )
    idx = off.astype("int64")
    keep = np.flatnonzero((idx >= 0) & (idx < axis.size))
    return idx[keep], keep


def tile_coverage(
    tile_lat: np.ndarray, tile_lon: np.ndarray
) -> tuple[slice, slice, np.ndarray, np.ndarray] | None:
    """源 tile 在目标网格上的落位。

    Returns
    -------
    ``(lat_slice, lon_slice, rows, cols)``，其中 ``rows``/``cols`` 是该 tile 内
    被采用的行/列号（框外部分已裁掉）。完全落在框外返回 ``None``。

    框外裁剪是设计意图：省库覆盖到 3.5N 的南海诸岛，而 ifs-hres 与 himawari
    本就在 15N 截断——ERA5 独自保留会造成源间不一致。
    """
    li, rows = _index_on_target(tile_lat, TARGET_LAT, "latitude")
    lj, cols = _index_on_target(tile_lon, TARGET_LON, "longitude")
    if li.size == 0 or lj.size == 0:
        return None
    if not (np.all(np.diff(li) == 1) and np.all(np.diff(lj) == 1)):
        raise ValueError("源 tile 坐标不连续——不支持带洞的 tile")
    return (
        slice(int(li[0]), int(li[-1]) + 1),
        slice(int(lj[0]), int(lj[-1]) + 1),
        rows,
        cols,
    )


def coverage_from_tiles(
    tile_coords: list[tuple[np.ndarray, np.ndarray]]
) -> np.ndarray:
    """目标网格上哪些格点有源数据。

    写进月库当坐标，供消费者区分「这里没数据」与「NaN 是因为夜间/缺测」——
    覆盖范围外返回 NaN 是设计意图，但不该让下游猜。
    """
    cov = np.zeros((TARGET_LAT.size, TARGET_LON.size), dtype=bool)
    for lat, lon in tile_coords:
        hit = tile_coverage(lat, lon)
        if hit is None:
            continue
        la, lo, _, _ = hit
        cov[la, lo] = True
    return cov


@dataclass
class Ledger:
    """``(月, 变量, 分片)`` 三元组的完成账本。

    加变量 = 只跑新变量那一列；中断 = 从账本续。

    **账本绑定 tile 集合指纹**：补下新 tile（台湾、沿海补丁）后重跑，若账本仍
    有效，所有分片都会被跳过——补下的数据不进库、而且不报错。故 tile 集合一变
    就整本作废、全量重烘（54 分片 × 240 月实测 13 分钟，不值得为省它冒静默
    陈旧的风险）。
    """

    path: Path
    _done: set[str] = field(default_factory=set)
    tiles_fingerprint: str | None = None

    @staticmethod
    def _key(month: str, var: str, shard_id: int) -> str:
        return f"{month}/{var}/{shard_id}"

    @classmethod
    def load(cls, path: Path, tiles_fingerprint: str | None = None) -> "Ledger":
        path = Path(path)
        if not path.exists():
            return cls(path, set(), tiles_fingerprint)
        raw = json.loads(path.read_text())
        stored = raw.get("tiles")
        if tiles_fingerprint is not None and stored != tiles_fingerprint:
            # stored 为 None = 指纹机制之前写的遗留账本，来历不明——同样作废。
            # 曾经放行过这种情况：结果 14 个新 tile 一个没进库，而新指纹还被
            # 写了进去，让账本声称自己是最新的（2026-08-12 实测踩到）。
            why = "无指纹（遗留账本）" if stored is None else f"指纹 {stored[:12]}"
            log.warning(
                "账本%s ≠ 当前 tile 集合 %s：账本作废，全量重烘。"
                "否则补下的新 tile 不会进库且不报错。",
                why, tiles_fingerprint[:12],
            )
            return cls(path, set(), tiles_fingerprint)
        return cls(path, set(raw["done"]), tiles_fingerprint or stored)

    def done(self, month: str, var: str, shard_id: int) -> bool:
        return self._key(month, var, shard_id) in self._done

    def mark(self, month: str, var: str, shard_id: int) -> None:
        self._done.add(self._key(month, var, shard_id))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"tiles": self.tiles_fingerprint, "done": sorted(self._done)},
                       indent=1),
            encoding="utf-8",
        )
