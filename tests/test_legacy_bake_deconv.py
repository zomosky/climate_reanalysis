"""中点化反解：solarcast 为 pvlib 做的 :30 搬移，烘焙时要还原回整点。

正演（era5.py::_to_interval_centre）：y[0]=x[0]，y[i]=0.5*(x[i-1]+x[i])，float32。
反解：x[0]=y[0]，x[i]=2*y[i]-x[i-1]。齐次解 (-1)^i*C 不衰减，故残差是否随长度
增长必须实测——不能靠推理。
"""

from __future__ import annotations

import numpy as np

from climate_reanalysis.legacy_bake.deconv import from_interval_centre


def to_interval_centre(a: np.ndarray) -> np.ndarray:
    """正演，逐字照抄 solarcast/era5.py::_to_interval_centre 的 float32 语义。"""
    c = a.copy()
    half = a.dtype.type(0.5)
    c[1:] = half * (a[:-1] + a[1:])
    return c


def _synthetic_t2m(n: int, seed: int = 0) -> np.ndarray:
    """真实感逐时气温：季节 + 日变化 + AR(1) 天气 + 白噪，float32。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    w = np.zeros(n)
    for i in range(1, n):
        w[i] = 0.97 * w[i - 1] + rng.normal(0, 0.8)
    return (
        288.0
        + 12 * np.sin(2 * np.pi * t / 8766)
        + 6 * np.sin(2 * np.pi * t / 24)
        + w
        + rng.normal(0, 0.15, n)
    ).astype(np.float32)


def test_first_sample_is_edge_held():
    x = _synthetic_t2m(16)
    y = to_interval_centre(x)
    assert from_interval_centre(y)[0] == y[0]


def test_exact_on_float64_roundtrip():
    """无量化误差时反解应精确（隔离掉 float32 舍入的影响）。"""
    x = _synthetic_t2m(512).astype(np.float64)
    y = x.copy()
    y[1:] = 0.5 * (x[:-1] + x[1:])
    np.testing.assert_allclose(from_interval_centre(y), x, rtol=0, atol=1e-9)


def test_residual_small_and_does_not_grow_over_20_years():
    """20 年逐时 175320 步：残差 < 0.02 K，且误差包络不随时间增长。

    交替模式不衰减，若实现有系统性偏差，包络会单调上升——这里逐 4 年检查。
    """
    n = 175320
    x = _synthetic_t2m(n)
    y = to_interval_centre(x)
    err = np.abs(from_interval_centre(y) - x)
    assert err.max() < 0.02
    envelope = [float(err[i : i + 8766].max()) for i in range(0, n - 8766, 8766 * 4)]
    assert max(envelope) < 0.02
    assert max(envelope) < 3 * min(envelope)  # 不增长（允许统计抖动）


def test_axis_argument_handles_3d_stacks():
    """烘焙时数组是 (time, lat, lon)，反解沿时间轴逐列独立进行。"""
    cols = np.stack([_synthetic_t2m(64, seed=s) for s in range(6)], axis=1)
    x = cols.reshape(64, 2, 3)
    y = x.copy()
    y[1:] = np.float32(0.5) * (x[:-1] + x[1:])
    got = from_interval_centre(y, axis=0)
    assert got.shape == x.shape
    np.testing.assert_allclose(got, x, rtol=0, atol=0.02)
