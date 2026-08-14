"""ERA5T 刷新：初步数据到期后重新拉一遍 final。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from climate_reanalysis.manifest import manifest_path, write_manifest
from climate_reanalysis.refresh import refresh, stale_months
from climate_reanalysis.store import ensure_store, set_provenance

AREA = (55.0, 70.0, 15.0, 140.0)


def _prov(store_dir: Path, month: str, var: str, expver):
    ensure_store(store_dir, month)
    entry = {"manifest_sha256": "sha", "source": "reanalysis-era5-single-levels"}
    if expver is not None:
        entry["expver"] = expver
    set_provenance(store_dir, month, var, entry)


def test_era5t_past_window_is_stale(tmp_path: Path):
    _prov(tmp_path / "s", "202601", "t2m", ["0005"])
    got = stale_months(tmp_path / "s", tmp_path / "m", dt.date(2026, 6, 1), 100)
    assert got == [("202601", "t2m")]


def test_era5t_within_window_is_not_stale(tmp_path: Path):
    """还没到 final 发布期就重下，只会再拿回一份初步数据，纯浪费配额。"""
    _prov(tmp_path / "s", "202601", "t2m", ["0005"])
    assert stale_months(tmp_path / "s", tmp_path / "m", dt.date(2026, 2, 20), 100) == []


def test_final_is_never_stale(tmp_path: Path):
    _prov(tmp_path / "s", "202601", "t2m", ["0001"])
    assert stale_months(tmp_path / "s", tmp_path / "m", dt.date(2027, 1, 1), 100) == []


def test_mixed_expver_is_stale(tmp_path: Path):
    """跨边界的月份含一段初步数据，整月都要重下才能保证一致。"""
    _prov(tmp_path / "s", "202601", "t2m", ["0001", "0005"])
    assert stale_months(tmp_path / "s", tmp_path / "m", dt.date(2026, 6, 1), 100) == [("202601", "t2m")]


def test_missing_expver_key_is_not_stale(tmp_path: Path):
    """老条目没记 expver——宁可漏刷，也不要把历史数据全部重下。"""
    _prov(tmp_path / "s", "202601", "t2m", None)
    assert stale_months(tmp_path / "s", tmp_path / "m", dt.date(2027, 1, 1), 100) == []


def test_dry_run_deletes_nothing(tmp_path: Path):
    md, sd = tmp_path / "m", tmp_path / "s"
    _prov(sd, "202601", "t2m", ["0005"])
    raw = tmp_path / "x.nc"
    raw.write_bytes(b"x")
    write_manifest(md, "202601", "t2m", [raw], AREA)
    got = refresh(sd, md, dt.date(2026, 6, 1), 100)          # execute 默认 False
    assert got == [("202601", "t2m")]
    assert manifest_path(md, "202601", "t2m").exists()        # 一个都没删


def test_execute_deletes_only_the_manifest(tmp_path: Path):
    """只删 manifest，月库数据一字不动——重建交给 restore 的 sha 比对自然触发。"""
    md, sd = tmp_path / "m", tmp_path / "s"
    _prov(sd, "202601", "t2m", ["0005"])
    raw = tmp_path / "x.nc"
    raw.write_bytes(b"x")
    write_manifest(md, "202601", "t2m", [raw], AREA)
    refresh(sd, md, dt.date(2026, 6, 1), 100, execute=True)
    assert not manifest_path(md, "202601", "t2m").exists()
    from climate_reanalysis.store import read_provenance
    assert read_provenance(sd, "202601")["t2m"]["expver"] == ["0005"]   # 月库没被动过
