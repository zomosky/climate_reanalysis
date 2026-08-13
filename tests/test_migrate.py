"""历史迁移：给既有月库的变量打 legacy 溯源，避免重下。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr

from climate_reanalysis.migrate import stamp_legacy
from climate_reanalysis.store import (
    ensure_store,
    read_provenance,
    store_path,
    write_variable,
)


def test_stamp_replaces_entry_without_touching_data(tmp_path: Path):
    """整条替换旧溯源（不残留无关旧字段）、不碰数据数组——前提是这条旧溯源本来就
    不含 ``manifest_sha256``（不是真实下载产物）。已有真实溯源该跳过，见
    ``test_skips_variables_with_real_manifest_provenance``。"""
    ensure_store(tmp_path, "202601")
    data = np.full((31 * 24, 161, 281), 2.0, dtype="float32")
    # 预置一条不含 manifest_sha256 的杂项旧溯源（例如上一次迁移用了不同的 tag）
    write_variable(tmp_path, "202601", "ssrd", data, {"note": "stale-tag"})
    write_variable(tmp_path, "202601", "t2m", data, {"note": "stale-tag"})

    n = stamp_legacy(tmp_path, ["202601"], ["ssrd", "t2m"], "legacy:solarcast-bake")
    assert n == 2
    prov = read_provenance(tmp_path, "202601")
    assert prov["ssrd"] == {"source": "legacy:solarcast-bake"}   # note 字段没有残留
    assert prov["t2m"] == {"source": "legacy:solarcast-bake"}

    ds = xr.open_zarr(store_path(tmp_path, "202601"), consolidated=True)
    np.testing.assert_allclose(ds["ssrd"].values, 2.0)            # 数据数组一字未动


def test_skips_variables_with_real_manifest_provenance(tmp_path: Path, capsys):
    """已有 ``manifest_sha256`` 的条目是这条管线正常下载 + restore 切割过的真实
    数据，不是历史遗留——``stamp_legacy`` 只补缺，不改写已有真实溯源。范围传参一旦
    与已下载月份重叠，无条件覆盖会把 manifest_sha256 换成 legacy 标记；此时若 raw
    文件已按 README 建议清理，后续 restore 想重建这个变量都做不到——数据没丢，但
    "怎么来的"这条线索丢了，且不会有任何报错。"""
    ensure_store(tmp_path, "202601")
    data = np.full((31 * 24, 161, 281), 2.0, dtype="float32")
    real_prov = {"manifest_sha256": "real-sha", "completed_at": "2026-01-01T00:00:00",
                "source": "reanalysis-era5-single-levels"}
    write_variable(tmp_path, "202601", "ssrd", data, real_prov)

    n = stamp_legacy(tmp_path, ["202601"], ["ssrd"], "legacy:solarcast-bake")
    assert n == 0
    assert read_provenance(tmp_path, "202601")["ssrd"] == real_prov   # 一字未改
    # 静默跳过等于让操作者以为迁移覆盖到了这个变量——必须出声。
    # structlog 用 PrintLoggerFactory 写 stdout，caplog 抓不到，得用 capsys。
    out = capsys.readouterr().out
    assert "skip_real_provenance" in out and "ssrd" in out


def test_fdir_gets_clipped_tag(tmp_path: Path):
    """历史 fdir 经过 min(fdir,ghi) 截断，与原生值严格讲不同源——须可见。"""
    ensure_store(tmp_path, "202601")
    stamp_legacy(tmp_path, ["202601"], ["fdir"], "legacy:solarcast-bake")
    assert read_provenance(tmp_path, "202601")["fdir"]["source"] == (
        "legacy:solarcast-bake(clipped)"
    )


def test_skips_missing_month(tmp_path: Path):
    assert stamp_legacy(tmp_path, ["209901"], ["ssrd"], "legacy:x") == 0
