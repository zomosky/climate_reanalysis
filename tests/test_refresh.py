"""ERA5T 刷新：初步数据到期后重新拉一遍 final。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from climate_reanalysis.archive import cold_path
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


def test_boundary_exactly_after_days_is_not_yet_stale(tmp_path: Path):
    """月末距今**正好等于** after_days 那天的边界：现有实现是 ``days <=
    after_days`` 视为未过期，这里把这条边界行为钉死——202601 月末
    （2026-01-31）+ 100 天 = 2026-05-11，当天还不过期；过了这天（次日）
    才算过期。"""
    _prov(tmp_path / "s", "202601", "t2m", ["0005"])
    assert stale_months(tmp_path / "s", tmp_path / "m", dt.date(2026, 5, 11), 100) == []
    assert stale_months(tmp_path / "s", tmp_path / "m", dt.date(2026, 5, 12), 100) == \
        [("202601", "t2m")]


def test_dry_run_deletes_nothing(tmp_path: Path):
    md, sd = tmp_path / "m", tmp_path / "s"
    _prov(sd, "202601", "t2m", ["0005"])
    raw = tmp_path / "x.nc"
    raw.write_bytes(b"x")
    write_manifest(md, "202601", "t2m", [raw], AREA)
    got = refresh(sd, md, dt.date(2026, 6, 1), 100)          # execute 默认 False
    assert got == [("202601", "t2m")]
    assert manifest_path(md, "202601", "t2m").exists()        # 一个都没删


def test_execute_deletes_manifest_and_raw_but_leaves_store_alone(tmp_path: Path):
    """--yes 必须真正触发重下：删 manifest 的同时也要删掉热端 raw 文件——否则
    download_one 的缓存判据 target.exists() 会命中同一份过期文件，一次网络
    请求都不发生，refresh 就是空转（这正是 Critical 2 修的那个 bug：此前只删
    manifest，raw 原样留着，"刷新"从未真正发生过）。月库数据（含 expver 溯源）
    本身不动——重建交给下一轮 download+restore 的 sha 比对自然触发。"""
    md, sd = tmp_path / "m", tmp_path / "s"
    _prov(sd, "202601", "t2m", ["0005"])
    raw = tmp_path / "x.nc"
    raw.write_bytes(b"x")
    write_manifest(md, "202601", "t2m", [raw], AREA)
    refresh(sd, md, dt.date(2026, 6, 1), 100, execute=True)
    assert not manifest_path(md, "202601", "t2m").exists()
    assert not raw.exists()          # 热端过期 raw 也被删了——不然重下无从发生
    from climate_reanalysis.store import read_provenance
    assert read_provenance(sd, "202601")["t2m"]["expver"] == ["0005"]   # 月库没被动过


def test_execute_deletes_cold_copy_too_when_configured(tmp_path: Path):
    """配了 cold_dir 时，对应的冷端副本同样要删——那份副本同样是过期的初步
    数据，留着只会是个陈旧的重复，而且 restore 的冷端回退可能读到它，让
    refresh 白刷（restore 发现热端缺失就回退去读那份同样过期的冷端数据）。"""
    md, sd, cd = tmp_path / "m", tmp_path / "s", tmp_path / "cold"
    _prov(sd, "202601", "t2m", ["0005"])
    raw = tmp_path / "raw" / "202601" / "x.nc"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"x")
    write_manifest(md, "202601", "t2m", [raw], AREA)
    cold = cold_path(cd, "202601", raw.name)
    cold.parent.mkdir(parents=True)
    cold.write_bytes(b"x")   # 模拟这份原始文件已经被 archive 搬过冷端一份

    refresh(sd, md, dt.date(2026, 6, 1), 100, execute=True, cold_dir=cd)

    assert not manifest_path(md, "202601", "t2m").exists()
    assert not raw.exists()
    assert not cold.exists()


def test_execute_without_cold_dir_only_touches_hot(tmp_path: Path):
    """cold_dir 未配置（出厂 None）时行为不受影响——只删热端 raw + manifest，
    不因为新逻辑而意外尝试碰任何冷端路径。"""
    md, sd = tmp_path / "m", tmp_path / "s"
    _prov(sd, "202601", "t2m", ["0005"])
    raw = tmp_path / "raw" / "202601" / "x.nc"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"x")
    write_manifest(md, "202601", "t2m", [raw], AREA)

    refresh(sd, md, dt.date(2026, 6, 1), 100, execute=True)   # cold_dir 默认 None

    assert not manifest_path(md, "202601", "t2m").exists()
    assert not raw.exists()


def test_refresh_then_download_actually_triggers_a_real_retrieve(tmp_path: Path, monkeypatch):
    """链路闭合：refresh --yes 之后跑一次 download，必须真的发起（打桩的）CDS
    请求——这是 Critical 2 要修的空转本身：此前 raw 没删，download_one 的缓存
    判据 target.exists() 直接命中，retrieve 一次都不会被调用，refresh 的存在
    毫无意义。这里断言 retrieve 确实被调用，而不只是信 refresh 自己的返回值。
    """
    from climate_reanalysis.download import download_one
    from climate_reanalysis.settings import Settings

    month, var, cds = "202601", "t2m", "2m_temperature"
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "m",
                store_dir=tmp_path / "s")
    ensure_store(s.store_dir, month)
    set_provenance(s.store_dir, month, var,
                   {"manifest_sha256": "sha", "expver": ["0005"]})
    raw = s.raw_dir / month / f"{cds}.nc"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"stale-preliminary-data")
    write_manifest(s.manifest_dir, month, var, [raw], AREA)

    refresh(s.store_dir, s.manifest_dir, dt.date(2026, 6, 1), 100, execute=True)

    calls: list[str] = []

    def _fake_retrieve(client, dataset, request, target, proxy, attempts=6, job_ids=None):
        calls.append(request["variable"][0])
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"fresh-final-data")
        return Path(target)

    monkeypatch.setattr("climate_reanalysis.download.retrieve", _fake_retrieve)

    download_one(s, client=None, month=month, var=var, today=dt.date(2026, 6, 2))

    assert calls == [cds]   # retrieve 真的被调用了——不再是零调用的空转
