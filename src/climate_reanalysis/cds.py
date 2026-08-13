"""CDS 客户端与可续传下载。

``cdsapi.retrieve()`` 在任何下载抖动上**重新排队整个作业**（服务器旧脚本日志实证：
"attempt 2 of 60, Retrying in 120 seconds"），在中欧链路上是灾难。改用
submit→poll→自己下载。

**代理只用于 bulk download，submit/poll 一律直连**：Python 的 requests 会读 macOS
的系统代理（Clash 会设它），故必须 ``trust_env = False``；curl 默认不读系统代理，
所以只有显式 ``-x`` 时才走代理。这个不对称正是"curl 能用而 Python 不能"的原因。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from climate_reanalysis.logging_setup import get_logger

log = get_logger(__name__)


def read_cdsapirc(path: Path | None = None) -> tuple[str, str]:
    """从 .cdsapirc 读 (url, key)。**只切第一个冒号**——url 的值自带 https://。"""
    p = Path(path or os.path.expanduser("~/.cdsapirc"))
    url = key = None
    for line in p.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if k == "url":
            url = v
        elif k == "key":
            key = v
    if not url or not key:
        raise ValueError(f"{p} 缺 url 或 key")
    return url, key


def make_client(settings):
    """直连的 datastores client（submit/poll 不走代理）。"""
    import ecmwf.datastores
    import requests

    url, key = read_cdsapirc(settings.cdsapirc)
    sess = requests.Session()
    sess.trust_env = False       # 同时忽略 env 与 macOS 系统代理
    return ecmwf.datastores.Client(url=url, key=key, session=sess,
                                   retry_after=60, maximum_tries=500)


def results_url(results) -> str:
    """对象存储的预签名 URL。

    **绝不能用 ``results.url``**——那是 /jobs/<id>/results 鉴权端点，curl 取会 401。
    """
    asset = getattr(results, "asset", None)
    if asset and asset.get("href"):
        return asset["href"]
    loc = getattr(results, "location", None)
    if loc:
        return loc
    raise RuntimeError("结果里没有对象存储 URL")


def _expected_size(url: str, proxy: str | None) -> int | None:
    """HEAD 一次问远端 Content-Length，用来判断下载有没有被静默截断。

    代理故障或链路抖动时，服务端/代理可能提前关闭连接，而 curl 把这当作
    正常结束、返回码仍是 0——只信 rc 会把半个文件当成功收下。``-C -``
    续传模式下 curl 自己的 %{size_download}/%{content_length_download}
    只反映"这一次尝试"下载/剩余的字节数，不是文件总长，没法拿来跟本地
    文件比对，所以总长必须另问远端一次。

    拿不到（HEAD 被拒绝、超时、没有 Content-Length 头——部分预签名 URL
    确实不支持 HEAD）时返回 None，调用方据此跳过大小校验、退回旧的
    "rc==0 即成功"判据：宁可放过一次校验，也不能让探测本身的网络问题
    把一次正常下载误判成失败。
    """
    import requests

    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = requests.head(url, proxies=proxies, timeout=30, allow_redirects=True)
        cl = resp.headers.get("Content-Length")
        return int(cl) if cl is not None else None
    except Exception as exc:  # noqa: BLE001 - 探测失败不阻断下载，只是校验退化
        log.warning("expected_size_probe_failed", url=url, error=str(exc))
        return None


def curl_download(url: str, target: Path, proxy: str | None,
                  attempts: int = 8) -> None:
    """可续传下载。``-C -`` 使断掉的传输接着传而不是重来。

    curl 返回码 0 不代表文件完整（见 ``_expected_size`` 的注释）——下载完
    额外比对本地文件大小与远端声明的总长，不够则视为失败继续重试（沿用
    已经落盘的部分续传，不是从头再来）。

    **写到 ``<target>.part``，通过大小校验后才 ``os.replace`` 成 target**：
    ``target.exists()`` 是下游（``download_one``）唯一的缓存/完整性判据，
    如果 curl 直接写 target，半途被杀（OOM、断电、docker 内存限制、或这里
    的重试耗尽）会在 target 位置留下一个残缺文件——下一次调用会把它当"已经
    下好"，连 size_bytes/sha256 一起原样写进 manifest，向下游撒谎。``.part``
    不受这个判据约束，可以放心留在磁盘供下次 ``-C -`` 续传；同目录内 rename
    是原子操作，不会出现"半个 target"的中间态。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.parent / f"{target.name}.part"
    expected = _expected_size(url, proxy)   # 只探测一次，url 在各次尝试间不变
    for i in range(1, attempts + 1):
        cmd = ["curl", "-sSL", "-C", "-", "--retry", "3", "--retry-delay", "5",
               "--retry-all-errors", "--connect-timeout", "30", "--fail"]
        if proxy:
            cmd += ["-x", proxy]
        cmd += ["-o", str(part), url]
        r = subprocess.run(cmd)
        have = part.stat().st_size if part.exists() else 0
        if r.returncode == 0 and (expected is None or have >= expected):
            os.replace(part, target)   # target 出现即代表完整，不会有中间态
            return
        if r.returncode == 0:
            log.warning("curl_size_mismatch", have=have, expected=expected,
                       attempt=i, target=str(target))
        else:
            log.warning("curl_failed", rc=r.returncode, attempt=i, target=str(target))
        if i < attempts:  # 最后一次失败后没有下一次尝试，不必再空等再报错
            time.sleep(min(10 * i, 60))
    raise RuntimeError(f"curl 下载失败（{attempts} 次）: {url}")


def retrieve(client, dataset: str, request: dict, target: Path,
             proxy: str | None, attempts: int = 6) -> Path:
    """submit → poll → 下载。每次重试都重新取 URL（预签名会过期）。"""
    remote = client.submit(dataset, request)
    while True:
        st = remote.status
        if st == "successful":
            break
        if st in ("failed", "rejected", "dismissed", "deleted"):
            raise RuntimeError(f"CDS job {st}: {dataset} {request.get('variable')}")
        time.sleep(30)
    last: Exception | None = None
    for _ in range(attempts):
        try:
            curl_download(results_url(remote.get_results()), target, proxy)
            return Path(target)
        except Exception as exc:  # noqa: BLE001 - 重取 URL 后再试
            last = exc
    raise RuntimeError(f"下载失败: {last}")
