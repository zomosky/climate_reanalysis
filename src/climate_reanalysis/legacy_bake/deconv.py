"""中点化的反解。

正演（``era5.py::_to_interval_centre``）把 :00 的瞬时值线性搬到 :30：
``y[0] = x[0]``，``y[i] = 0.5 * (x[i-1] + x[i])``，全程 float32。这是为 pvlib
让所有量共用一根轴而做的——在 solarcast 那侧是正确的对齐（区间平均通量的代表
时刻是区间中点，PV 模型要用中点的太阳角重建 DNI 与倾斜面转置）。

但 zrread 把同一件对齐做在**查询时**（由 ``zrread_flux`` 触发 Δ/2 平移与区间
积分），所以落盘必须回到 ERA5 原生整点轴，否则中点标记会让 kt 分母的平均窗口
整体错位 30 分钟（晨昏实测 20-80% 误差）。

反解 ``x[i] = 2*y[i] - x[i-1]`` 的齐次解是 ``(-1)^i * C``，**不衰减**，故其稳定性
必须实测而非推理：20 年（175320 步、float32 正演）残差最大 0.015 K、中位
0.008 K，且误差包络不随时间增长——驱动只是 float32 舍入的随机游走
（``2ε√n ≈ 0.014 K``，与实测吻合）。相对 ERA5 自身 0.5-1 K 的精度可视为无损。
"""

from __future__ import annotations

import numpy as np


def from_interval_centre(y: np.ndarray, axis: int = 0) -> np.ndarray:
    """把中点化（:30）的数组还原为原始整点（:00）值。

    Parameters
    ----------
    y
        中点化后的数组，任意维度。
    axis
        时间轴（省库为 0）。

    Returns
    -------
    np.ndarray
        与 ``y`` 同形状的 ``float64`` 数组。递推在 float64 下进行：递推本身不
        放大误差，但用 float32 会在每一步额外注入舍入。
    """
    a = np.moveaxis(np.asarray(y, dtype="float64"), axis, 0)
    out = np.empty_like(a)
    out[0] = a[0]  # 正演对首点是边界保持（y[0] = x[0]），故首点精确
    for i in range(1, a.shape[0]):
        out[i] = 2.0 * a[i] - out[i - 1]
    return np.moveaxis(out, 0, axis)
