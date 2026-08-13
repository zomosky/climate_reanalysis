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


def curl_download(url: str, target: Path, proxy: str | None,
                  attempts: int = 8) -> None:
    """可续传下载。``-C -`` 使断掉的传输接着传而不是重来。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    for i in range(1, attempts + 1):
        cmd = ["curl", "-sSL", "-C", "-", "--retry", "3", "--retry-delay", "5",
               "--retry-all-errors", "--connect-timeout", "30", "--fail"]
        if proxy:
            cmd += ["-x", proxy]
        cmd += ["-o", str(target), url]
        r = subprocess.run(cmd)
        if r.returncode == 0:
            return
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
