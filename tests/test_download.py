"""downloader：就绪判据、请求形态、失败粒度、缓存复用。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from climate_reanalysis.download import (
    build_request,
    download_many,
    download_one,
    raw_path,
)
from climate_reanalysis.manifest import manifest_path
from climate_reanalysis.settings import Settings

TODAY = dt.date(2026, 3, 10)          # 2026-01/02 均已齐全


def _settings(tmp_path: Path) -> Settings:
    return Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "m",
                    store_dir=tmp_path / "s", dl_proxy=None)


class FakeClient:
    """打桩 CDS：记录请求，按 fail_on 决定某个变量是否失败。"""

    def __init__(self, fail_on: set[str] | None = None):
        self.requests: list[tuple[str, dict]] = []
        self.fail_on = fail_on or set()


def _fake_retrieve(monkeypatch, client: FakeClient):
    def rv(cl, dataset, request, target, proxy, attempts=6):
        cl.requests.append((dataset, request))
        v = request["variable"][0]
        if v in cl.fail_on:
            raise RuntimeError(f"CDS job failed: {v}")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"nc" * 10)
        return Path(target)

    monkeypatch.setattr("climate_reanalysis.download.retrieve", rv)


def test_request_shape_is_month_and_area(monkeypatch, tmp_path: Path):
    req = build_request("2m_temperature", "202601", (55.0, 70.0, 15.0, 140.0))
    assert req["product_type"] == ["reanalysis"]
    assert req["year"] == ["2026"] and req["month"] == ["01"]
    assert len(req["day"]) == 31 and len(req["time"]) == 24
    assert req["area"] == [55.0, 70.0, 15.0, 140.0]


def test_not_ready_month_is_skipped(monkeypatch, tmp_path: Path):
    s = _settings(tmp_path)
    c = FakeClient()
    _fake_retrieve(monkeypatch, c)
    # 2026-03 是当月，永远不就绪
    assert download_one(s, c, "202603", "t2m", TODAY) is None
    assert c.requests == []
    assert not manifest_path(s.manifest_dir, "202603", "t2m").exists()


def test_success_writes_manifest_and_raw(monkeypatch, tmp_path: Path):
    s = _settings(tmp_path)
    c = FakeClient()
    _fake_retrieve(monkeypatch, c)
    p = download_one(s, c, "202601", "t2m", TODAY)
    assert p == manifest_path(s.manifest_dir, "202601", "t2m")
    assert p.exists()
    assert raw_path(s.raw_dir, "202601", "2m_temperature").exists()


def test_si10_requests_two_cds_variables_one_manifest(monkeypatch, tmp_path: Path):
    s = _settings(tmp_path)
    c = FakeClient()
    _fake_retrieve(monkeypatch, c)
    download_one(s, c, "202601", "si10", TODAY)
    assert len(c.requests) == 2
    import json
    d = json.loads(manifest_path(s.manifest_dir, "202601", "si10").read_text())
    assert len(d["files"]) == 2


def test_one_failing_variable_does_not_affect_others(monkeypatch, tmp_path: Path):
    """上一轮 45 个 tile 因一个不可得变量全批失败——粒度必须限制在单变量。"""
    s = _settings(tmp_path)
    c = FakeClient(fail_on={"2m_temperature"})
    _fake_retrieve(monkeypatch, c)
    ok, failed = download_many(s, c, ["202601"], ["t2m", "sp"], TODAY)
    assert [f[1] for f in failed] == ["t2m"]
    assert manifest_path(s.manifest_dir, "202601", "sp").exists()
    assert not manifest_path(s.manifest_dir, "202601", "t2m").exists()


def test_cached_raw_is_not_redownloaded(monkeypatch, tmp_path: Path):
    s = _settings(tmp_path)
    c = FakeClient()
    _fake_retrieve(monkeypatch, c)
    download_one(s, c, "202601", "sp", TODAY)
    n = len(c.requests)
    manifest_path(s.manifest_dir, "202601", "sp").unlink()   # 只删信号，留缓存
    download_one(s, c, "202601", "sp", TODAY)
    assert len(c.requests) == n          # 未重复请求


def test_cleanup_deletes_failed_jobs(monkeypatch, tmp_path: Path):
    """失败 job 占配额直到删除——批次收尾必须清理。"""
    from climate_reanalysis.download import cleanup_failed_jobs

    deleted: list[str] = []

    class C:
        def get_jobs(self, limit, status):
            from types import SimpleNamespace
            return SimpleNamespace(request_ids=[f"{status}-1", f"{status}-2"])

        def delete(self, *ids):
            deleted.extend(ids)

    assert cleanup_failed_jobs(C()) == 4
    assert set(deleted) == {"failed-1", "failed-2", "rejected-1", "rejected-2"}


def test_cleanup_survives_api_errors(monkeypatch):
    """清理失败不得影响下载结果。"""
    from climate_reanalysis.download import cleanup_failed_jobs

    class C:
        def get_jobs(self, limit, status):
            raise RuntimeError("422")

    assert cleanup_failed_jobs(C()) == 0


def test_cleanup_batches_deletes_in_groups_of_50(monkeypatch):
    """267 个失败 job 一次性清过——分页大小必须真的按 50 分批，不能一把梭。"""
    from climate_reanalysis.download import cleanup_failed_jobs

    calls: list[int] = []

    class C:
        def get_jobs(self, limit, status):
            from types import SimpleNamespace
            n = 60 if status == "failed" else 60   # 合计 120，跨 3 批（50/50/20）
            return SimpleNamespace(request_ids=[f"{status}-{i}" for i in range(n)])

        def delete(self, *ids):
            assert len(ids) <= 50
            calls.append(len(ids))

    assert cleanup_failed_jobs(C()) == 120
    assert calls == [50, 50, 20]
