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
from climate_reanalysis.store import check_area_matches_grid
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
                 today: dt.date | None = None,
                 job_ids: list[str] | None = None) -> Path | None:
    """下载一个（月×产出变量）。未就绪返回 None；成功返回 manifest 路径。

    ``job_ids``：透传给 ``retrieve``，用来记录本轮提交的 CDS request_id
    （见 ``cleanup_failed_jobs`` 为什么只清理本轮自己的任务）。
    """
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
                     target, settings.dl_proxy, job_ids=job_ids)
        files.append(target)
    return write_manifest(settings.manifest_dir, month, var, files, settings.area)


def _download_one_result(settings, client, month: str, var: str,
                         today: dt.date | None, job_ids: list[str]):
    """并发 worker：把 download_one 的结果/异常规整成一个三元组返回。

    worker 线程只产出结果，不直接碰共享的 ok/failed 列表——归并放在主线程的
    as_completed 循环里做单写者，天然不需要加锁。``job_ids`` 是个例外：多个
    worker 会并发 append 同一个列表，但 list.append 在 CPython 里是单条原子
    字节码，GIL 下天然不会产生撕裂状态，不需要额外加锁。
    """
    try:
        path = download_one(settings, client, month, var, today, job_ids=job_ids)
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
    踩踏同一批 job id。**只清理本轮 submit 得到的 job_ids**——同账号上可能正
    跑着别的批次（例如另一段历史回填），账号级全清是个危险操作，见
    ``cleanup_account_failed_jobs`` 的显式独立入口。即使本轮全部缓存命中、
    一个都没下（job_ids 为空），也照样调用一次——只是没有 id 可清，天然是
    个空操作，不需要特判。

    启动期先校验 ``settings.area`` 与契约网格是否吻合（``check_area_matches_grid``）
    ——改错 area 不会在下载时报错，要等整月下载完进 restore 才会报"网格不符"，
    代价是白白花掉的 CDS 配额和数小时带宽；这里几乎零成本，在提交任何真实
    请求之前就把配置错误拦下来。
    """
    check_area_matches_grid(settings.area)
    ok: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    job_ids: list[str] = []
    tasks = [(month, var) for month in months for var in variables]
    workers = max(1, settings.concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_download_one_result, settings, client, month, var,
                               today, job_ids)
                  for month, var in tasks]
        for fut in as_completed(futures):
            kind, month, var, payload = fut.result()
            if kind == "ok":
                ok.append((month, var))
            elif kind == "failed":
                failed.append((month, var, payload))
            # kind == "skip"：未就绪，既不算成功也不算失败
    cleanup_failed_jobs(client, job_ids)
    return ok, failed


def _list_failed_ids(client, statuses, scope: set[str] | None) -> list[str]:
    """列出 CDS 上处于 ``statuses`` 的 job id；``scope`` 非空时只保留其中的
    id（『本轮』语义的交集）；``scope=None`` 是账号级——不做任何过滤。

    两个 API 细节：``status`` 传 list 会被序列化成空值而 422，必须逐个传；
    ``page.next`` 生成的下一页 URL 会丢掉 status 值、且异常延迟到下一轮访问
    ``request_ids`` 时才抛（绕过翻页处的 try），故改用一次大 limit。
    """
    ids: list[str] = []
    for st in statuses:
        try:
            found = list(client.get_jobs(limit=2000, status=st).request_ids)
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响下载结果
            log.warning("job_list_failed", status=st, error=str(exc))
            continue
        if scope is not None:
            found = [i for i in found if i in scope]
        ids.extend(found)
    return ids


def _delete_ids(client, ids: list[str]) -> None:
    for i in range(0, len(ids), 50):
        try:
            client.delete(*ids[i:i + 50])
        except Exception as exc:  # noqa: BLE001
            log.warning("job_delete_failed", error=str(exc))


def cleanup_failed_jobs(client, job_ids: list[str], statuses=("failed", "rejected")) -> int:
    """只删除**本轮自己提交**的失败/被拒 job（``job_ids``：本轮 submit 得到的
    全部 request_id），释放排队配额。

    同账号上可能正跑着别的下载任务（例如另一段历史回填）——不限本批的账号级
    全清是个危险操作，见 ``cleanup_account_failed_jobs``；``download_many`` 的
    收尾只应该调用这个作用域受限的版本。
    """
    ids = _list_failed_ids(client, statuses, set(job_ids))
    _delete_ids(client, ids)
    if ids:
        log.info("cleaned_failed_jobs", count=len(ids))
    return len(ids)


def cleanup_account_failed_jobs(client, statuses=("failed", "rejected")) -> int:
    """账号级清理：删除该账号上**全部**失败/被拒 job，不限本批。

    危险操作——同账号上可能正跑着别的下载任务（例如另一段历史回填），会被
    一并删掉。**不要**在下载收尾时自动调用；只作为显式、独立的运维入口
    （CLI ``cleanup-jobs`` 子命令，默认走 ``preview_account_failed_jobs`` 的
    dry-run，只有显式加 ``--yes`` 才会调用本函数真正执行），需要时手动触发。
    """
    ids = _list_failed_ids(client, statuses, None)
    _delete_ids(client, ids)
    if ids:
        log.info("cleaned_failed_jobs_account_wide", count=len(ids))
    return len(ids)


def preview_account_failed_jobs(
    client, statuses=("failed", "rejected")
) -> dict[str, list[str]]:
    """``cleanup_account_failed_jobs`` 的 dry-run 预览：按状态分类列出**将会**
    被删除的 job id，不做任何实际删除。账号级清理是不可撤销操作、同账号可能
    正跑着别人的任务（例如一段 20 年历史回填），敲错账号/敲错配置即刻执行没
    有任何找回办法——CLI ``cleanup-jobs`` 默认走这个函数，必须显式加 ``--yes``
    才会转去调用真正执行删除的 ``cleanup_account_failed_jobs``。
    """
    return {st: _list_failed_ids(client, [st], None) for st in statuses}
