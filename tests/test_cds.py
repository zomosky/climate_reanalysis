"""CDS 客户端与可续传下载（全部打桩，零真实网络）。"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from climate_reanalysis.cds import curl_download, read_cdsapirc, results_url
from climate_reanalysis.settings import Settings


def test_read_cdsapirc_splits_on_first_colon_only(tmp_path: Path):
    """url 的值自带 https://，只能切第一个冒号。"""
    rc = tmp_path / "cdsapirc"
    rc.write_text("url: https://cds.climate.copernicus.eu/api\nkey: abc:def\n")
    url, key = read_cdsapirc(rc)
    assert url == "https://cds.climate.copernicus.eu/api"
    assert key == "abc:def"


def test_results_url_prefers_asset_href():
    """必须取 asset['href']——res.url 是需要鉴权的 /results 端点，会 401。"""
    res = SimpleNamespace(asset={"href": "https://obj/x.zip"},
                          location="https://other", url="https://api/jobs/1/results")
    assert results_url(res) == "https://obj/x.zip"


def test_results_url_falls_back_to_location():
    res = SimpleNamespace(asset=None, location="https://obj/y.zip",
                          url="https://api/jobs/1/results")
    assert results_url(res) == "https://obj/y.zip"


# --------------------------------------------------------------------------- #
# curl 返回码 0 不代表文件完整：代理静默截断连接时 curl 仍可能报 rc=0（把提前
# 关闭的连接当作正常结束）。所以 curl_download 必须额外问一次远端 Content-
# Length（HEAD），下载后比对本地文件大小，不够就当失败走重试——而不是只信 rc。
#
# 注意 ``-C -`` 续传下 curl 自己的 %{size_download}/%{content_length_download}
# 只反映"这一次尝试"的字节数，不是文件总长，不能拿来跟本地文件比——所以校验
# 总长必须另问远端一次，测试里用 requests.head 打桩模拟这个远端应答。
# --------------------------------------------------------------------------- #
def _stub_head(monkeypatch, content_length: int | None = None):
    """打桩 requests.head，避免 curl_download 内部的大小校验发出真实 HEAD
    请求。content_length=None 模拟远端不给 Content-Length（校验被跳过，
    退回『rc==0 即成功』）；给数字则模拟服务器声明的对象总长度。"""
    import requests
    headers = {} if content_length is None else {"Content-Length": str(content_length)}
    monkeypatch.setattr(requests, "head", lambda url, **kw: SimpleNamespace(headers=headers))


def test_curl_download_passes_proxy_and_resume_flags(monkeypatch, tmp_path: Path):
    _stub_head(monkeypatch)   # 不关心大小校验，跳过（无 Content-Length）
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"data")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = tmp_path / "out.bin"
    curl_download("https://obj/x.zip", target, proxy="http://127.0.0.1:7890")
    assert "-C" in seen["cmd"] and "-" in seen["cmd"]      # 断点续传
    assert "-x" in seen["cmd"]
    assert "http://127.0.0.1:7890" in seen["cmd"]
    assert target.read_bytes() == b"data"


def test_curl_download_omits_proxy_when_none(monkeypatch, tmp_path: Path):
    _stub_head(monkeypatch)   # 不关心大小校验，跳过（无 Content-Length）
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"d")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    curl_download("https://obj/x.zip", tmp_path / "o.bin", proxy=None)
    assert "-x" not in seen["cmd"]


def test_curl_download_raises_after_attempts(monkeypatch, tmp_path: Path):
    # 失败重试之间会 sleep 退避；打桩掉 time.sleep 而不是真的等，测试要快。
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _stub_head(monkeypatch)   # 不关心大小校验，跳过（无 Content-Length）
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=18))
    with pytest.raises(RuntimeError, match="curl"):
        curl_download("https://obj/x.zip", tmp_path / "o.bin", proxy=None, attempts=2)


def test_curl_download_retries_when_rc_zero_but_file_too_short(monkeypatch, tmp_path: Path):
    """核心回归用例：curl 说成功（rc=0）但文件比远端声明的总长短——必须不认账，
    继续走重试（-C - 续传，不是从头下）而不是原样收下一个残缺文件。"""
    _stub_head(monkeypatch, content_length=100)
    monkeypatch.setattr(time, "sleep", lambda s: None)   # 重试间会退避，测试不真等
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        target = Path(cmd[cmd.index("-o") + 1])
        if len(calls) == 1:
            target.write_bytes(b"x" * 40)          # 被截断：只有 40/100 字节
        else:
            with target.open("ab") as f:            # 续传：接着补满
                f.write(b"y" * 60)
        return SimpleNamespace(returncode=0)         # curl 自己认为成功了

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = tmp_path / "out.bin"
    curl_download("https://obj/x.zip", target, proxy=None)

    assert len(calls) == 2                           # 第一次"成功"但太短，必须重试
    assert target.read_bytes() == b"x" * 40 + b"y" * 60
    assert target.stat().st_size == 100


def test_curl_download_raises_if_persistently_short(monkeypatch, tmp_path: Path):
    """反复都拿不到完整大小——用尽 attempts 后必须报错，不能把残缺文件当成功。"""
    _stub_head(monkeypatch, content_length=100)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def fake_run(cmd, **kw):
        target = Path(cmd[cmd.index("-o") + 1])
        target.write_bytes(b"x" * 40)   # 每次都只给 40/100 字节，rc 却是 0
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="curl"):
        curl_download("https://obj/x.zip", tmp_path / "o.bin", proxy=None, attempts=2)


# --------------------------------------------------------------------------- #
# target.exists() 是 download_one 的缓存判据（"文件在=已完整"）。如果 curl 直接
# 写 target，半途被杀（OOM/断电/curl 重试耗尽）会在 target 位置留下一个残缺文件——
# 下次 cron 起来会把这个残缺文件当成"已经下好"，连 size_bytes/sha256 一起原样
# 写进 manifest，向下游撒谎。必须写到 <target>.part，通过大小校验才 rename 成
# target，使"target 存在"与"完整"重新等价；.part 仍留在磁盘供下次 -C - 续传。
# --------------------------------------------------------------------------- #
def test_curl_download_writes_to_part_then_atomic_rename(monkeypatch, tmp_path: Path):
    _stub_head(monkeypatch)   # 不关心大小校验，跳过（无 Content-Length）
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        out = Path(cmd[cmd.index("-o") + 1])
        assert out.name.endswith(".part")   # 必须写到 .part，不能直接写 target
        out.write_bytes(b"data")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = tmp_path / "out.bin"
    curl_download("https://obj/x.zip", target, proxy=None)

    assert target.read_bytes() == b"data"
    assert not (tmp_path / "out.bin.part").exists()   # 成功后 rename，不留 .part


def test_curl_download_never_leaves_target_when_interrupted(monkeypatch, tmp_path: Path):
    """中断（这里用"耗尽 attempts"模拟同一后果）：target 绝不出现，残缺数据
    留在 .part 里供下次续传——不能凭 target.exists() 误判成已完整。"""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _stub_head(monkeypatch, content_length=100)

    def fake_run(cmd, **kw):
        out = Path(cmd[cmd.index("-o") + 1])
        out.write_bytes(b"x" * 40)   # 每次都只给 40/100，rc 却是 0
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = tmp_path / "out.bin"
    with pytest.raises(RuntimeError, match="curl"):
        curl_download("https://obj/x.zip", target, proxy=None, attempts=2)

    assert not target.exists()
    assert (tmp_path / "out.bin.part").exists()   # 残缺数据留着，供下次 -C - 续传


def test_curl_download_resumes_from_part_file_left_by_a_previous_process(
    monkeypatch, tmp_path: Path
):
    """模拟"上一轮进程被杀、.part 留在磁盘"的场景（新调用 = 下一次 cron）：
    curl -C - 应当接着传（用 fake_run 对已存在 .part 的 append 模拟），
    最终成功产出完整 target 且 .part 被清走。"""
    _stub_head(monkeypatch, content_length=100)
    part = tmp_path / "out.bin.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"x" * 40)   # 模拟上一轮遗留的残缺 .part

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = Path(cmd[cmd.index("-o") + 1])
        assert out == part          # 必须接着写同一个 .part，不是另起炉灶
        with out.open("ab") as f:   # -C - 续传：接着补
            f.write(b"y" * 60)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = tmp_path / "out.bin"
    curl_download("https://obj/x.zip", target, proxy=None)

    assert len(calls) == 1
    assert target.read_bytes() == b"x" * 40 + b"y" * 60
    assert not part.exists()


# --------------------------------------------------------------------------- #
# make_client：session 的 trust_env 必须是 False——这是「curl 能用而 Python 不能」
# 这条坑的核心断言，不能只验证「调用了就算过」。真的构造 ecmwf.datastores.Client()
# 会在 __attrs_post_init__ 里发一次真实探测请求，所以必须打桩掉 Client 本身，
# 绝不能让测试触达真实网络。
# --------------------------------------------------------------------------- #
def test_make_client_forces_trust_env_false(monkeypatch, tmp_path: Path):
    import ecmwf.datastores
    import requests

    rc = tmp_path / "cdsapirc"
    rc.write_text("url: https://cds.example.org/api\nkey: my-secret-key\n")

    captured = {}

    class FakeSession:
        def __init__(self):
            self.trust_env = True  # requests.Session 的真实默认值

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(requests, "Session", FakeSession)
    monkeypatch.setattr(ecmwf.datastores, "Client", FakeClient)

    from climate_reanalysis.cds import make_client

    settings = Settings(cdsapirc=rc)
    make_client(settings)

    assert captured["url"] == "https://cds.example.org/api"
    assert captured["key"] == "my-secret-key"
    assert captured["session"].trust_env is False   # 关键断言：显式关闭，不是默认值
    assert captured["retry_after"] == 60
    assert captured["maximum_tries"] == 500


# --------------------------------------------------------------------------- #
# retrieve：submit → poll → 下载，且失败重试之间重新取 URL（预签名会过期）。
# 用假 client/remote 打桩，不碰真实 ecmwf.datastores；time.sleep 打桩掉避免
# 真的等 30s 一次轮询。
# --------------------------------------------------------------------------- #
def test_retrieve_polls_until_successful_then_downloads(monkeypatch, tmp_path: Path):
    import climate_reanalysis.cds as cds

    monkeypatch.setattr(cds.time, "sleep", lambda s: None)

    class FakeRemote:
        def __init__(self):
            self._statuses = iter(["queued", "running", "successful"])

        @property
        def status(self):
            return next(self._statuses)

        def get_results(self):
            return SimpleNamespace(asset={"href": "https://obj/final.zip"})

    class FakeClient:
        def submit(self, dataset, request):
            assert dataset == "reanalysis-era5-single-levels"
            assert request == {"variable": ["t2m"]}
            return FakeRemote()

    calls = []

    def fake_curl_download(url, target, proxy, attempts=8):
        calls.append(url)
        Path(target).write_bytes(b"ok")

    monkeypatch.setattr(cds, "curl_download", fake_curl_download)

    target = tmp_path / "out.nc"
    result = cds.retrieve(FakeClient(), "reanalysis-era5-single-levels",
                          {"variable": ["t2m"]}, target, proxy=None)

    assert result == target
    assert calls == ["https://obj/final.zip"]      # 未成功前不下载
    assert target.read_bytes() == b"ok"


def test_retrieve_raises_on_terminal_failure(monkeypatch, tmp_path: Path):
    """failed/rejected/dismissed/deleted 是终态——必须报错，不能一直轮询。"""
    import climate_reanalysis.cds as cds

    monkeypatch.setattr(cds.time, "sleep", lambda s: None)

    class FakeRemote:
        def __init__(self):
            self._statuses = iter(["accepted", "rejected"])

        @property
        def status(self):
            return next(self._statuses)

    class FakeClient:
        def submit(self, dataset, request):
            return FakeRemote()

    with pytest.raises(RuntimeError, match="rejected"):
        cds.retrieve(FakeClient(), "ds", {"variable": ["t2m"]},
                     tmp_path / "x.nc", proxy=None)


def test_retrieve_refetches_url_between_download_attempts(monkeypatch, tmp_path: Path):
    """每次下载重试都要重新 get_results() 拿新 URL——预签名 URL 会过期，
    复用旧 URL 重试是无意义的（曾经踩过的坑）。"""
    import climate_reanalysis.cds as cds

    monkeypatch.setattr(cds.time, "sleep", lambda s: None)

    class FakeRemote:
        def __init__(self):
            self.get_results_calls = 0

        @property
        def status(self):
            return "successful"

        def get_results(self):
            self.get_results_calls += 1
            return SimpleNamespace(asset={"href": f"https://obj/try{self.get_results_calls}.zip"})

    remote = FakeRemote()

    class FakeClient:
        def submit(self, dataset, request):
            return remote

    calls = []

    def fake_curl_download(url, target, proxy, attempts=8):
        calls.append(url)
        if len(calls) < 2:
            raise RuntimeError("模拟第一次下载失败（如 URL 已过期）")
        Path(target).write_bytes(b"ok")

    monkeypatch.setattr(cds, "curl_download", fake_curl_download)

    target = tmp_path / "out.nc"
    result = cds.retrieve(FakeClient(), "ds", {"variable": ["t2m"]}, target,
                          proxy=None, attempts=3)

    assert result == target
    # 两次下载尝试用的是两个不同 URL——证明重试确实重新取过 URL
    assert calls == ["https://obj/try1.zip", "https://obj/try2.zip"]
    assert remote.get_results_calls == 2
    assert target.read_bytes() == b"ok"


def test_retrieve_raises_after_exhausting_download_attempts(monkeypatch, tmp_path: Path):
    import climate_reanalysis.cds as cds

    monkeypatch.setattr(cds.time, "sleep", lambda s: None)

    class FakeRemote:
        @property
        def status(self):
            return "successful"

        def get_results(self):
            return SimpleNamespace(asset={"href": "https://obj/always-fails.zip"})

    class FakeClient:
        def submit(self, dataset, request):
            return FakeRemote()

    def always_fail(url, target, proxy, attempts=8):
        raise RuntimeError("curl 一直失败")

    monkeypatch.setattr(cds, "curl_download", always_fail)

    with pytest.raises(RuntimeError, match="下载失败"):
        cds.retrieve(FakeClient(), "ds", {"variable": ["t2m"]},
                     tmp_path / "x.nc", proxy=None, attempts=2)
