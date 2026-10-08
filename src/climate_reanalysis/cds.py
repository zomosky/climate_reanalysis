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
from collections.abc import Callable
from pathlib import Path

from climate_reanalysis.logging_setup import get_logger

log = get_logger(__name__)


class TransferError(RuntimeError):
    """Safe transfer diagnostics; the saved CDS job and partial file remain usable."""

    def __init__(self, rc: int | None, have: int, expected: int | None, *, grew=False):
        self.rc, self.have, self.expected, self.grew = rc, have, expected, grew
        super().__init__(f'curl transfer incomplete: rc={rc}, bytes={have}/{expected}; '
                         'partial retained for resume')


def results_size(results) -> int | None:
    """Prefer CDS asset metadata to another unreliable object-store HEAD."""
    value = getattr(results, 'content_length', None)
    if value is None:
        value = (getattr(results, 'asset', None) or {}).get('file:size')
    if value is None:
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise ValueError('invalid CDS result size') from None
    if size <= 0:
        raise ValueError('invalid CDS result size')
    return size


def _curl_config_value(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace(
        '\n', '\\n').replace('\r', '\\r') + '"'


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


def _direct_session():
    """trust_env=False 的 requests.Session：忽略 env 与 macOS 系统代理。

    submit/poll（``make_client``）与 HEAD 探测（``_expected_size``）共用同一条
    直连约定——探测走漏了这条约定，代理不通时探测请求会被代理吞掉/挂起，
    异常又被 ``_expected_size`` 自己的 except 悄悄吃掉，等于把截断校验关掉
    而不自知。
    """
    import requests

    sess = requests.Session()
    sess.trust_env = False
    return sess


def _make_client(rc: Path | None):
    import ecmwf.datastores

    url, key = read_cdsapirc(rc)
    sess = _direct_session()
    return ecmwf.datastores.Client(url=url, key=key, session=sess,
                                   retry_after=60, maximum_tries=500)


def make_client(settings):
    """直连的 datastores client（submit/poll 不走代理）。"""
    return _make_client(settings.cdsapirc)


def make_clients(settings):
    """账号池：``settings.cdsapircs`` 每份 rc 一个直连 client（顺序对应）；
    空列表 = 单账号，退回 ``cdsapirc``——没配池的既有用法不受影响。

    防单账号限流的思路承自 solarcast（多开进程、每进程 SOLARCAST_CDSAPIRC
    指一个账号）；收拢进本项目后是一次运行内的进程内账号池，任务分发见
    ``download.download_many_accounts``。"""
    rcs = list(settings.cdsapircs) or [settings.cdsapirc]
    return [_make_client(rc) for rc in rcs]


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
    把一次正常下载误判成失败。**退化必须出声**——探测失败意味着这次下载
    的截断校验此刻是关的，静默吞掉异常会让人误以为校验一直在生效。

    HEAD 用跟 submit/poll 一样的 ``trust_env=False`` 直连 session（``proxies``
    参数只是「显式覆盖」，``trust_env=True`` 时 requests 仍会在它之外读取
    macOS 系统代理/环境变量）——否则本机 Clash 之类的系统代理会插到这次探测
    请求上；代理不通时探测异常，被这里的 except 吃掉退化成"rc==0 即成功"，
    等于把 Task 6 好不容易加上的截断校验悄悄关掉，而调用方毫不知情。
    """
    try:
        sess = _direct_session()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = sess.head(url, proxies=proxies, timeout=30, allow_redirects=True)
        cl = resp.headers.get("Content-Length")
        return int(cl) if cl is not None else None
    except Exception as exc:  # noqa: BLE001 - 探测失败不阻断下载，只是校验退化——但必须出声
        log.warning("expected_size_probe_failed", error_type=type(exc).__name__)
        return None


def curl_download(url: str, target: Path, proxy: str | None,
                  attempts: int = 8, *, strict_size: bool = False,
                  expected_bytes: int | None = None,
                  refresh_results: Callable[[], object] | None = None) -> None:
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
    expected = expected_bytes if expected_bytes is not None else _expected_size(url, proxy)
    if strict_size and (expected is None or expected <= 0):
        raise ValueError("remote size unavailable; refusing unchecked managed download")
    initial = part.stat().st_size if part.exists() else 0
    rc = None
    for i in range(1, attempts + 1):
        before = part.stat().st_size if part.exists() else 0
        if strict_size and before > expected:
            raise ValueError('partial file exceeds remote size; preserved for inspection')
        # A previous process may have received every byte but died before rename.
        if strict_size and before == expected:
            os.replace(part, target)
            return
        if i > 1 and refresh_results is not None:
            try:
                results = refresh_results()
                refreshed_size = results_size(results)
                url = results_url(results)
            except Exception:
                raise TransferError(None, before, expected, grew=before > initial) from None
            if refreshed_size is not None:
                if expected is not None and refreshed_size != expected:
                    raise ValueError('remote size changed; partial preserved for inspection')
                expected = refreshed_size
        # curl's internal retries can discard new bytes and reuse the old offset.
        # Each invocation instead computes -C - afresh, with a bounded duration.
        # Ignore curlrc and implicit environment proxies. Signed URLs and proxy
        # credentials go through stdin, never process arguments or exception text.
        cmd = ["curl", "-q", "-sSL", "-C", "-", "--connect-timeout", "30",
               "--max-time", "300", "--fail", "--config", "-", "-o", str(part)]
        config = 'url = ' + _curl_config_value(url) + '\n'
        if proxy:
            config += 'proxy = ' + _curl_config_value(proxy) + '\n'
            cmd += ['--noproxy', '']
        else:
            cmd += ['--noproxy', '*']
        r = subprocess.run(cmd, input=config, text=True, capture_output=True)
        rc = r.returncode
        have = part.stat().st_size if part.exists() else 0
        if have < before:
            raise ValueError('partial file shrank during transfer; refusing publication')
        if strict_size and have > expected:
            raise ValueError('partial file exceeds remote size; preserved for inspection')
        size_ok = expected is None or (have == expected if strict_size else have >= expected)
        if rc == 0 and size_ok:
            os.replace(part, target)   # target 出现即代表完整，不会有中间态
            return
        if rc == 0:
            log.warning("curl_size_mismatch", have=have, expected=expected,
                       attempt=i, target=str(target))
        else:
            log.warning("curl_failed", rc=rc, attempt=i, before=before, have=have,
                        expected=expected, target=str(target))
        if i < attempts:  # 最后一次失败后没有下一次尝试，不必再空等再报错
            time.sleep(min(10 * i, 60))
    have = part.stat().st_size if part.exists() else 0
    raise TransferError(rc, have, expected, grew=have > initial)


def retrieve(client, dataset: str, request: dict, target: Path,
             proxy: str | None, attempts: int = 6,
             job_ids: list[str] | None = None) -> Path:
    """submit → poll → 下载。每次重试都重新取 URL（预签名会过期）。

    ``job_ids``：submit 成功后立即把 ``remote.request_id`` 追加进这个调用方
    提供的列表——不论这次 submit 最终成功还是失败，都要记下来，好让批次
    收尾时"只清理本轮自己提交的失败 job"（见 download.cleanup_failed_jobs）
    有据可查，而不必对账号做一次全量扫描。
    """
    remote = client.submit(dataset, request)
    if job_ids is not None:
        job_ids.append(remote.request_id)
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
