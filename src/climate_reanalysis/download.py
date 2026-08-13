"""单元 A：按（月 × 产出变量）下载并写 manifest。

**失败粒度限制在单个（月×变量）**：上一轮把一个不可得变量（ssrdc 在 ARCO 端点无
数据）排进批次，导致 45 个 tile 全批失败、连同已下好的三个可得变量一起丢弃。一个
变量的失败不得影响同月其它变量——单文件变量如此，si10 这种由多个 CDS 变量拼成
一个产出变量的情形同样如此（一个成功一个失败也不得写出半份 manifest）。

manifest 只在该产出变量的**全部** CDS 文件都下好后才写——它是唯一的就绪信号。

``download_many`` 按 ``settings.concurrency`` 用线程池并发（原因见其自身
docstring）；失败隔离与"只在成功时写"这两条契约在并发路径下同样成立。
"""

from __future__ import annotations

import calendar
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from climate_reanalysis.cds import retrieve
from climate_reanalysis.logging_setup import get_logger
from climate_reanalysis.manifest import manifest_path, write_manifest
from climate_reanalysis.period import is_ready
from climate_reanalysis.variables import cds_variables

log = get_logger(__name__)

DATASET = "reanalysis-era5-single-levels"


def raw_path(raw_dir: Path, month: str, cds_var: str) -> Path:
    return Path(raw_dir) / month / f"{cds_var}.nc"


def build_request(cds_var: str, month: str, area) -> dict:
    """一个（月 × CDS 变量 × 中国框）的请求。"""
    y, m = int(month[:4]), int(month[4:6])
    ndays = calendar.monthrange(y, m)[1]
    return {
        "product_type": ["reanalysis"],
        "variable": [cds_var],
        "year": [f"{y:04d}"],
        "month": [f"{m:02d}"],
        "day": [f"{d:02d}" for d in range(1, ndays + 1)],
        "time": [f"{h:02d}:00" for h in range(24)],
        "area": list(area),
        "data_format": "netcdf",
        "download_format": "unarchived",
    }


def download_one(settings, client, month: str, var: str,
                 today: dt.date | None = None) -> Path | None:
    """下载一个（月×产出变量）。未就绪返回 None；成功返回 manifest 路径。"""
    today = today or dt.date.today()
    if not is_ready(month, today, settings.publish_lag_days):
        log.info("skip_not_ready", month=month, var=var)
        return None
    existing = manifest_path(settings.manifest_dir, month, var)
    if existing.exists():
        return existing
    files: list[Path] = []
    for cds_var in cds_variables(var):
        target = raw_path(settings.raw_dir, month, cds_var)
        if not target.exists():          # 缓存命中则不重复请求
            retrieve(client, DATASET, build_request(cds_var, month, settings.area),
                     target, settings.dl_proxy)
        files.append(target)
    return write_manifest(settings.manifest_dir, month, var, files, settings.area)


def _download_one_result(settings, client, month: str, var: str,
                         today: dt.date | None):
    """并发 worker：把 download_one 的结果/异常规整成一个三元组返回。

    worker 线程只产出结果，不直接碰共享的 ok/failed 列表——归并放在主线程的
    as_completed 循环里做单写者，天然不需要加锁。
    """
    try:
        path = download_one(settings, client, month, var, today)
    except Exception as exc:  # noqa: BLE001 - 单个失败不中断整批，交回主线程记录
        log.error("download_failed", month=month, var=var, error=str(exc))
        return "failed", month, var, str(exc)
    return ("ok", month, var, path) if path is not None else ("skip", month, var, None)


def download_many(settings, client, months: list[str], variables: list[str],
                  today: dt.date | None = None):
    """逐（月×变量）下载，按 ``settings.concurrency`` 并发。单个失败记录后
    继续——不影响同月其它变量。

    并发的意义不在带宽而在排队重叠：CDS 每个请求排队常要 1-1.5 小时，顺序跑
    时这段等待完全串行；多个请求同时在飞行，等待期彼此重叠。**默认值 4 别调
    高**——同时排队的请求太多会触发 CDS 的 "queued requests temporarily
    limited"（实测新账号 8 个并发提交会整批先 accepted 后 rejected）；链路
    退化（日志频现 curl rc=18）时按 settings.concurrency 的运维指引调低到 1-2。

    cleanup_failed_jobs 在 ThreadPoolExecutor 的 ``with`` 块退出（等价于全部
    worker 已经 join）之后才调用一次——每个 worker 各清一次既浪费又可能相互
    踩踏同一批 job id。
    """
    ok: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    tasks = [(month, var) for month in months for var in variables]
    workers = max(1, settings.concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_download_one_result, settings, client, month, var, today)
                  for month, var in tasks]
        for fut in as_completed(futures):
            kind, month, var, payload = fut.result()
            if kind == "ok":
                ok.append((month, var))
            elif kind == "failed":
                failed.append((month, var, payload))
            # kind == "skip"：未就绪，既不算成功也不算失败
    cleanup_failed_jobs(client)
    return ok, failed


def cleanup_failed_jobs(client, statuses=("failed", "rejected")) -> int:
    """删除 CDS 上的失败/被拒 job，释放排队配额。

    两个 API 细节：``status`` 传 list 会被序列化成空值而 422，必须逐个传；
    ``page.next`` 生成的下一页 URL 会丢掉 status 值、且异常延迟到下一轮访问
    ``request_ids`` 时才抛（绕过翻页处的 try），故改用一次大 limit。
    """
    ids: list[str] = []
    for st in statuses:
        try:
            ids.extend(list(client.get_jobs(limit=2000, status=st).request_ids))
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响下载结果
            log.warning("job_list_failed", status=st, error=str(exc))
    for i in range(0, len(ids), 50):
        try:
            client.delete(*ids[i:i + 50])
        except Exception as exc:  # noqa: BLE001
            log.warning("job_delete_failed", error=str(exc))
    if ids:
        log.info("cleaned_failed_jobs", count=len(ids))
    return len(ids)
