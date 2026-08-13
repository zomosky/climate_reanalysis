"""downloader：就绪判据、请求形态、失败粒度、缓存复用、并发上限。"""

from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path

import pytest

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
    def rv(cl, dataset, request, target, proxy, attempts=6, job_ids=None):
        cl.requests.append((dataset, request))
        v = request["variable"][0]
        if job_ids is not None:
            job_ids.append(f"job-{v}")
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


def test_download_many_rejects_area_that_drifted_from_grid(monkeypatch, tmp_path: Path):
    """settings.area 与 store.TARGET_LAT/TARGET_LON 是两份独立的真值——改错
    area 不会在下载时报错，要等整月下载完进 restore 才报"网格不符"，代价是
    白白花掉的 CDS 配额和数小时带宽。必须在提交任何真实请求之前就拦下来：
    这里断言一个请求都没发出去（c.requests 为空），不只是断言最终抛了错。"""
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "m",
                store_dir=tmp_path / "s", dl_proxy=None,
                area=(55.0, 70.0, 16.0, 140.0))   # S 打错了一度，不再匹配契约网格
    c = FakeClient()
    _fake_retrieve(monkeypatch, c)

    with pytest.raises(ValueError, match="网格"):
        download_many(s, c, ["202601"], ["t2m"], TODAY)

    assert c.requests == []


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


def test_existing_manifest_short_circuits_even_when_raw_was_cleaned_up(
    monkeypatch, tmp_path: Path
):
    """「manifest 存在即早退」是保护 CDS 配额的闸门：raw 在 restore 成功后按
    README 建议会被清理，此时唯一还在的信号是 manifest。若这条早退失效，
    download_one 会退到逐 cds_var 检查 raw 是否存在——raw 已删、必然不存在——
    从而对已经处理过的 (月×变量) 重新发起一次真实 CDS 请求，等于每次 cron
    都重下已有数据。这条用例必须只靠 manifest 存在就短路，不依赖 raw 是否还在。"""
    s = _settings(tmp_path)
    c = FakeClient()
    _fake_retrieve(monkeypatch, c)
    download_one(s, c, "202601", "t2m", TODAY)
    assert len(c.requests) == 1
    raw_path(s.raw_dir, "202601", "2m_temperature").unlink()   # 模拟 restore 后清理 raw

    p = download_one(s, c, "202601", "t2m", TODAY)

    assert p == manifest_path(s.manifest_dir, "202601", "t2m")
    assert len(c.requests) == 1        # 没有发起第二次真实请求


def test_multi_file_variable_partial_failure_writes_no_manifest(monkeypatch, tmp_path: Path):
    """si10 = u10 + v10：一个成功一个失败——"只在成功时写 manifest"这条唯一契约
    必须对多文件变量也成立，不能因为已经拿到一半就侥幸写出。"""
    s = _settings(tmp_path)
    c = FakeClient(fail_on={"10m_v_component_of_wind"})
    _fake_retrieve(monkeypatch, c)
    ok, failed = download_many(s, c, ["202601"], ["si10"], TODAY)
    assert ok == []
    assert [f[1] for f in failed] == ["si10"]
    assert not manifest_path(s.manifest_dir, "202601", "si10").exists()
    # 已成功那一半（u10）的 raw 文件确实落了盘——但没有 manifest 就不算数，
    # 下次调用会重新走到 v10 那一步再试一次（u10 因缓存命中不会重下）。
    assert raw_path(s.raw_dir, "202601", "10m_u_component_of_wind").exists()
    assert not raw_path(s.raw_dir, "202601", "10m_v_component_of_wind").exists()


def test_cleanup_account_wide_deletes_all_failed_jobs(monkeypatch, tmp_path: Path):
    """账号级清理（显式独立入口，不由 download_many 自动调用）：失败 job 占配额
    直到删除，且不限本批——这是它跟 cleanup_failed_jobs 的区别，见下方
    test_cleanup_scoped_only_deletes_this_rounds_ids。"""
    from climate_reanalysis.download import cleanup_account_failed_jobs

    deleted: list[str] = []

    class C:
        def get_jobs(self, limit, status):
            from types import SimpleNamespace
            return SimpleNamespace(request_ids=[f"{status}-1", f"{status}-2"])

        def delete(self, *ids):
            deleted.extend(ids)

    assert cleanup_account_failed_jobs(C()) == 4
    assert set(deleted) == {"failed-1", "failed-2", "rejected-1", "rejected-2"}


def test_cleanup_account_wide_survives_api_errors(monkeypatch):
    """清理失败不得影响下载结果。"""
    from climate_reanalysis.download import cleanup_account_failed_jobs

    class C:
        def get_jobs(self, limit, status):
            raise RuntimeError("422")

    assert cleanup_account_failed_jobs(C()) == 0


def test_cleanup_account_wide_batches_deletes_in_groups_of_50(monkeypatch):
    """267 个失败 job 一次性清过——分页大小必须真的按 50 分批，不能一把梭。"""
    from climate_reanalysis.download import cleanup_account_failed_jobs

    calls: list[int] = []

    class C:
        def get_jobs(self, limit, status):
            from types import SimpleNamespace
            n = 60 if status == "failed" else 60   # 合计 120，跨 3 批（50/50/20）
            return SimpleNamespace(request_ids=[f"{status}-{i}" for i in range(n)])

        def delete(self, *ids):
            assert len(ids) <= 50
            calls.append(len(ids))

    assert cleanup_account_failed_jobs(C()) == 120
    assert calls == [50, 50, 20]


def test_preview_account_wide_lists_by_status_without_deleting(monkeypatch):
    """cleanup-jobs CLI 默认走的 dry-run 预览：按状态分类列出将要删除的 id，
    绝不调用 delete——账号级清理零防呆、敲错即刻不可撤销，预览路径必须从
    结构上不可能碰到真删。

    ``delete`` 调用记进列表而不是直接 raise：``_delete_ids`` 本身会 try/except
    吞掉 delete 抛出的异常（清理失败不该影响下载结果），若这里改成 raise，
    误删也会被那层 except 悄悄咽掉、测不出来——必须靠事后断言列表为空。
    """
    from climate_reanalysis.download import preview_account_failed_jobs

    deleted: list[str] = []

    class C:
        def get_jobs(self, limit, status):
            from types import SimpleNamespace
            return SimpleNamespace(request_ids=[f"{status}-1", f"{status}-2"])

        def delete(self, *ids):
            deleted.extend(ids)

    preview = preview_account_failed_jobs(C())

    assert preview == {
        "failed": ["failed-1", "failed-2"],
        "rejected": ["rejected-1", "rejected-2"],
    }
    assert deleted == []


def test_cleanup_scoped_only_deletes_this_rounds_ids(monkeypatch):
    """cleanup_failed_jobs（download_many 收尾调用的那个）只能删 job_ids 范围内
    的——同账号上可能正跑着别的批次，账号级返回的失败/被拒列表里混着不属于
    本轮的 id，必须被过滤掉，不能一并删除。"""
    from climate_reanalysis.download import cleanup_failed_jobs

    deleted: list[str] = []

    class C:
        def get_jobs(self, limit, status):
            from types import SimpleNamespace
            # 服务器上同时有本轮（-mine）和别的批次（-other）留下的失败/被拒 job
            return SimpleNamespace(request_ids=[f"{status}-mine", f"{status}-other"])

        def delete(self, *ids):
            deleted.extend(ids)

    n = cleanup_failed_jobs(C(), job_ids=["failed-mine", "rejected-mine"])

    assert n == 2
    assert set(deleted) == {"failed-mine", "rejected-mine"}   # "-other" 一个都没碰


def test_cleanup_scoped_with_no_submissions_is_a_noop(monkeypatch):
    """本轮全部缓存命中、一个都没下（job_ids 为空）——收尾照样调用一次，但
    天然清不出任何东西，不需要特判成"跳过"。"""
    from climate_reanalysis.download import cleanup_failed_jobs

    class C:
        def get_jobs(self, limit, status):
            from types import SimpleNamespace
            return SimpleNamespace(request_ids=[f"{status}-someone-elses"])

        def delete(self, *ids):
            raise AssertionError("job_ids 为空时不该删任何东西")

    assert cleanup_failed_jobs(C(), job_ids=[]) == 0


# --------------------------------------------------------------------------- #
# settings.concurrency：并发的意义不在带宽而在排队重叠——CDS 每个请求排队常要
# 1-1.5 小时，顺序跑时这段等待完全串行；多个请求同时在飞行，等待期彼此重叠。
# 不依赖时序断言具体重叠了几次（会 flaky），只钉住"峰值不超过配置上限"这条
# 硬约束，以及"concurrency=1 时确实退化成顺序"。
# --------------------------------------------------------------------------- #
def _peak_tracking_retrieve(monkeypatch, state: dict, lock: threading.Lock,
                            delay: float = 0.05, fail_on: set[str] | None = None):
    """打桩 retrieve：记录同时在飞行的调用数峰值，用 sleep 撑开窗口让并发有
    机会真的重叠（否则 GIL + 瞬时假 I/O 可能巧合地表现成顺序，测试会假阳性）。"""
    fail_on = fail_on or set()

    def rv(cl, dataset, request, target, proxy, attempts=6, job_ids=None):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        time.sleep(delay)
        with lock:
            state["current"] -= 1
        v = request["variable"][0]
        if job_ids is not None:
            job_ids.append(f"job-{v}")
        if v in fail_on:
            raise RuntimeError(f"CDS job failed: {v}")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"nc")
        return Path(target)

    monkeypatch.setattr("climate_reanalysis.download.retrieve", rv)


def test_download_many_respects_concurrency_limit(monkeypatch, tmp_path: Path):
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "m",
                store_dir=tmp_path / "s", dl_proxy=None, concurrency=4)
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}
    _peak_tracking_retrieve(monkeypatch, state, lock)
    c = FakeClient()
    variables = ["t2m", "sp", "d2m", "u100", "v100", "si10"]   # 够撑开并发窗口
    download_many(s, c, ["202601"], variables, TODAY)

    assert state["peak"] <= 4       # 硬约束：绝不超过配置上限
    assert state["peak"] > 1        # 真的重叠过，不是巧合地退化成顺序


def test_download_many_is_sequential_when_concurrency_is_one(monkeypatch, tmp_path: Path):
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "m",
                store_dir=tmp_path / "s", dl_proxy=None, concurrency=1)
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}
    _peak_tracking_retrieve(monkeypatch, state, lock, delay=0.02)
    c = FakeClient()
    download_many(s, c, ["202601"], ["t2m", "sp", "d2m"], TODAY)

    assert state["peak"] == 1       # concurrency=1 不能借并发框架偷偷跑并行


def test_concurrent_failure_does_not_affect_others(monkeypatch, tmp_path: Path):
    """并发路径下失败隔离必须依然成立：一个变量失败不影响同批其它变量。"""
    s = _settings(tmp_path)   # concurrency 默认 4
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}
    _peak_tracking_retrieve(monkeypatch, state, lock, delay=0.02,
                            fail_on={"2m_temperature"})
    c = FakeClient()
    ok, failed = download_many(s, c, ["202601"], ["t2m", "sp", "d2m", "u100"], TODAY)

    assert sorted(o[1] for o in ok) == ["d2m", "sp", "u100"]
    assert [f[1] for f in failed] == ["t2m"]
    assert not manifest_path(s.manifest_dir, "202601", "t2m").exists()
    for v in ("sp", "d2m", "u100"):
        assert manifest_path(s.manifest_dir, "202601", v).exists()
