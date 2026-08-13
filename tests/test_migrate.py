"""历史迁移：给既有月库的变量打 legacy 溯源，避免重下。"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from climate_reanalysis.migrate import stamp_legacy
from climate_reanalysis.store import ensure_store, read_provenance, write_variable


def test_stamp_marks_variables_without_touching_data(tmp_path: Path):
    ensure_store(tmp_path, "202601")
    data = np.full((31 * 24, 161, 281), 2.0, dtype="float32")
    write_variable(tmp_path, "202601", "ssrd", data, {"manifest_sha256": "old"})

    n = stamp_legacy(tmp_path, ["202601"], ["ssrd", "t2m"], "legacy:solarcast-bake")
    assert n == 2
    prov = read_provenance(tmp_path, "202601")
    assert prov["ssrd"]["source"] == "legacy:solarcast-bake"
    assert prov["t2m"]["source"] == "legacy:solarcast-bake"
    assert "manifest_sha256" not in prov["ssrd"]     # legacy 无 manifest


def test_fdir_gets_clipped_tag(tmp_path: Path):
    """历史 fdir 经过 min(fdir,ghi) 截断，与原生值严格讲不同源——须可见。"""
    ensure_store(tmp_path, "202601")
    stamp_legacy(tmp_path, ["202601"], ["fdir"], "legacy:solarcast-bake")
    assert read_provenance(tmp_path, "202601")["fdir"]["source"] == (
        "legacy:solarcast-bake(clipped)"
    )


def test_skips_missing_month(tmp_path: Path):
    assert stamp_legacy(tmp_path, ["209901"], ["ssrd"], "legacy:x") == 0
