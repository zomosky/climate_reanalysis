"""ERA5T 刷新：把过期的初步数据（expver=0005）换成 final（expver=0001）。

**只删 manifest，绝不碰月库数据**——这是刻意的设计，不是偷懒。管线的两个单元
（download 写 manifest、restore 读 manifest 切库）只通过 manifest 通信，这是
它们之间唯一的契约；`refresh` 不去教 download 认得月库结构，只是把过期月份的
manifest 删掉，让既有的 download → restore 流程自然把它们当成"没下过"重新走
一遍——sha 天然对不上，restore 自动重建，两个单元本身零改动。

真正的重下发生在**下一次** `download` + `restore`，`refresh` 自己不联网、不写
任何新文件，只做"清单预览"和"删 manifest"两件事。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from climate_reanalysis.logging_setup import get_logger
from climate_reanalysis.manifest import manifest_path
from climate_reanalysis.store import read_provenance

log = get_logger(__name__)

#: ERA5T 初步数据的 expver 标记（final 是 "0001"，见 restore.py 的记法）
_PRELIMINARY = "0005"

#: store_path 落盘文件名的固定后缀，用来从目录反推月份 token
_STORE_SUFFIX = "_era5.zarr"


def _month_end(month: str) -> dt.date:
    """该月最后一天。与 ``period.is_ready`` 判"是否已发布"用途不同（那是
    "月末 + lag_days 是否已过、能不能认为该月齐全"），这里判的是"距今过了多久、
    该不该重新拉一遍"，故不合并复用。"""
    y, m = int(month[:4]), int(month[4:6])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return dt.date(ny, nm, 1) - dt.timedelta(days=1)


def _iter_months(store_dir: Path) -> list[str]:
    """列出月库目录里已存在的月份 token（按文件名反推，排序），目录不存在则
    为空列表——`refresh` 在没建过任何月库的全新环境里应当是安全的空操作。"""
    d = Path(store_dir)
    if not d.is_dir():
        return []
    return sorted(
        p.name[: -len(_STORE_SUFFIX)]
        for p in d.glob(f"*{_STORE_SUFFIX}")
    )


def stale_months(
    store_dir: Path, manifest_dir: Path, today: dt.date, after_days: int
) -> list[tuple[str, str]]:
    """扫描月库溯源，找出该重新拉一遍 final 的 (月, 变量)。

    判据：该 (月, 变量) 的溯源里 ``expver`` 含 ``"0005"``（说明这段数据至少
    有一部分仍是 ERA5T 初步值），且该月月末距 ``today`` 已超过 ``after_days``
    天（final 通常滞后 2-3 个月才发布，等太短只会再拿回一份初步数据）。

    **缺 ``expver`` 键的老条目视为不需要刷新**——`migrate` 打的 legacy 标记、
    更早版本写入的溯源都没有这个字段，宁可漏刷这部分，也不要把没记录 expver
    的历史数据当成待刷新对象，那会是几十小时排队和几十 GB 带宽的代价。

    ``manifest_dir`` 目前不参与判据（判据只看月库溯源），保留在签名里是为了
    和 ``refresh()`` 对齐、供未来扩展（比如要求真正下载过、manifest 确实存在
    的月份才算数）时不必改调用方。
    """
    del manifest_dir  # 当前判据不需要，见 docstring
    out: list[tuple[str, str]] = []
    for month in _iter_months(store_dir):
        if (today - _month_end(month)).days <= after_days:
            continue
        prov = read_provenance(store_dir, month)
        for var in sorted(prov):
            expver = prov[var].get("expver")
            if expver and _PRELIMINARY in expver:
                out.append((month, var))
    return out


def refresh(
    store_dir: Path,
    manifest_dir: Path,
    today: dt.date,
    after_days: int,
    execute: bool = False,
) -> list[tuple[str, str]]:
    """把过期 ERA5T 月份的 manifest 删掉，交给下一轮 download + restore 重下。

    ``execute=False``（默认）只返回将要刷新的清单，**不删任何文件**——删
    manifest 会触发真实重下，代价是排队时间和带宽，必须先看清楚清单再决定。
    ``execute=True`` 才真正删除，且只删 manifest 本身，绝不碰月库数据本身
    （重建交给后续 restore 按 sha 不符自然触发，不在这里抢它的活）。

    对应 manifest 本就不存在的 (月, 变量)（例如只打过 legacy 标记、从未真正
    走过 download 的历史月）静默跳过，不计入"实际删除"的返回值，也不报错——
    没有 manifest 可删本身不是错误。
    """
    stale = stale_months(store_dir, manifest_dir, today, after_days)
    if not execute:
        return stale
    deleted: list[tuple[str, str]] = []
    for month, var in stale:
        p = manifest_path(manifest_dir, month, var)
        if not p.exists():
            log.warning("refresh_manifest_missing", month=month, var=var, path=str(p))
            continue
        p.unlink()
        deleted.append((month, var))
    return deleted
