"""单元 A：按（月 × 产出变量）下载并写 manifest。

**失败粒度限制在单个（月×变量）**：上一轮把一个不可得变量（ssrdc 在 ARCO 端点无
数据）排进批次，导致 45 个 tile 全批失败、连同已下好的三个可得变量一起丢弃。一个
变量的失败不得影响同月其它变量。

manifest 只在该产出变量的**全部** CDS 文件都下好后才写——它是唯一的就绪信号。
"""

from __future__ import annotations

import calendar
import datetime as dt
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


def download_many(settings, client, months: list[str], variables: list[str],
                  today: dt.date | None = None):
    """逐（月×变量）下载。单个失败记录后继续——不影响同月其它变量。"""
    ok: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    for month in months:
        for var in variables:
            try:
                if download_one(settings, client, month, var, today) is not None:
                    ok.append((month, var))
            except Exception as exc:  # noqa: BLE001 - 单个失败不中断整批
                log.error("download_failed", month=month, var=var, error=str(exc))
                failed.append((month, var, str(exc)))
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
