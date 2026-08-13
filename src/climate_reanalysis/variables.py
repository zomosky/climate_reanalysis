"""产出变量 ↔ CDS 变量的映射、换算与 kind 标注。

产出变量名与单位由 zrread 已在读的月库契约固定（见 spec §2.1/§5.4），本模块是
CDS 原生形态到该契约的唯一转换点。

**一个产出变量可能来自多个 CDS 变量**（``si10 = hypot(u10, v10)``），故 manifest
的粒度按**产出变量**——一个产出变量要么齐、要么不发布。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np


@dataclass(frozen=True)
class VarSpec:
    cds: tuple[str, ...]
    units: str
    step_type: str          # "avg"（区间平均通量，标签在右边界）/ "instant"
    convert: Callable[..., np.ndarray]


def _flux(a: np.ndarray) -> np.ndarray:
    """J/m²（过去一小时累积）-> W/m² 区间平均通量；辐照不得为负。"""
    return np.clip(np.asarray(a, dtype="float64") / 3600.0, 0.0, None)


def _same(a: np.ndarray) -> np.ndarray:
    return np.asarray(a, dtype="float64")


def _hypot(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    return np.hypot(np.asarray(u, dtype="float64"), np.asarray(v, dtype="float64"))


VAR_SPECS: dict[str, VarSpec] = {
    "ssrd":  VarSpec(("surface_solar_radiation_downwards",), "W m**-2", "avg", _flux),
    "fdir":  VarSpec(("total_sky_direct_solar_radiation_at_surface",),
                     "W m**-2", "avg", _flux),
    "ssrdc": VarSpec(("surface_solar_radiation_downward_clear_sky",),
                     "W m**-2", "avg", _flux),
    "t2m":   VarSpec(("2m_temperature",), "K", "instant", _same),
    "d2m":   VarSpec(("2m_dewpoint_temperature",), "K", "instant", _same),
    "u100":  VarSpec(("100m_u_component_of_wind",), "m s**-1", "instant", _same),
    "v100":  VarSpec(("100m_v_component_of_wind",), "m s**-1", "instant", _same),
    "sp":    VarSpec(("surface_pressure",), "Pa", "instant", _same),
    "si10":  VarSpec(("10m_u_component_of_wind", "10m_v_component_of_wind"),
                     "m s**-1", "instant", _hypot),
}


def cds_variables(name: str) -> tuple[str, ...]:
    """该产出变量需要请求的 CDS 变量（可能不止一个）。"""
    return VAR_SPECS[name].cds


def convert(name: str, arrays: dict[str, np.ndarray]) -> np.ndarray:
    """按 CDS 变量名取数组、套用换算，返回产出变量的值。"""
    spec = VAR_SPECS[name]
    return spec.convert(*(arrays[c] for c in spec.cds))
