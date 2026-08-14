"""ERA5T 刷新：把过期的初步数据（expver=0005）换成 final（expver=0001）。

**绝不碰月库数据**——这是刻意的设计，不是偷懒。管线的两个单元（download 写
manifest、restore 读 manifest 切库）只通过 manifest 通信，这是它们之间唯一的
契约；`refresh` 不去教 download 认得月库结构，只是把过期月份的 manifest（连同
对应的原始文件）删掉，让既有的 download → restore 流程自然把它们当成"没下过"
重新走一遍——sha 天然对不上，restore 自动重建，两个单元本身零改动。

**必须连原始文件（raw，热端 + 冷端）一起删，不能只删 manifest**：`download_one`
判断"这个 (月×变量) 还需不需要真的发请求"分两层——manifest 存在就整体早退，
manifest 不存在则逐 CDS 变量看 `target.exists()`（热端 raw 在不在）决定要不要
调用 `retrieve`。只删 manifest 的话，第二层缓存判据原封不动地命中同一份过期
raw 文件，`download_one` 会在**零网络请求**的情况下把这份旧文件原样重新写进
manifest（`completed_at` 变了，sha 因此也变，看起来像是"处理过"），
`restore` 因此重建出跟之前一模一样的初步数据——`refresh` 于是变成一次
"空转"：日志照常打 `refresh_done`，但 `expver` 永远还是 `0005`，永远刷不动。
ERA5T 月份恰恰是**最不可能已经归档**的最近月份，这条空转会命中几乎全部目标。

删除顺序**先 raw（热端 + 冷端）、最后 manifest**，不能反过来：manifest 存在
是"这份数据已完整"的信号，它最后消失，中途崩溃时留下的是"manifest 在但 raw
没了"——这个状态下 `restore` 只要月库溯源还在就照常跳过，无害；反过来先删
manifest 会留下一个无主的 raw 文件（既不被任何 manifest 引用，也不会被当前
的 `download_one` 缓存判据用到，纯粹是垃圾）。

真正的重下发生在**下一次** `download` + `restore`，`refresh` 自己不联网、只在
`execute=True` 时才写（删）文件，`execute=False`（默认）只做"清单预览"。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from climate_reanalysis.logging_setup import get_logger
from climate_reanalysis.manifest import manifest_path, read_manifest
from climate_reanalysis.paths import cold_path
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
    cold_dir: Path | None = None,
) -> list[tuple[str, str]]:
    """把过期 ERA5T 月份的 manifest**连同原始文件**删掉，交给下一轮
    download + restore 重下。

    ``execute=False``（默认）只返回将要刷新的清单，**不删任何文件**——删
    manifest 与 raw 会触发真实重下，代价是排队时间和带宽，必须先看清楚清单
    再决定。``execute=True`` 才真正删除，且**只删 manifest 与 raw 这两类
    "重下即可再生"的文件**，绝不碰月库数据本身（重建交给后续 restore 按 sha
    不符自然触发，不在这里抢它的活）。

    删除顺序：**先 raw（热端 + 若配置了 cold_dir 则连冷端也删），最后才删
    manifest**——manifest 是"这份数据已完整下载"的信号，必须最后消失，理由
    见模块顶部 docstring。``cold_dir`` 为 ``None``（未配置归档）时不做任何
    冷端相关操作，只删热端 raw，行为与未接入冷存归档之前一致。

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
        d = read_manifest(p)
        for f in d["files"]:
            hot = Path(f["path"])
            if hot.exists():
                hot.unlink()
            if cold_dir is not None:
                cold = cold_path(Path(cold_dir), month, hot.name)
                if cold.exists():
                    cold.unlink()
        # manifest 最后删——它是"已完整下载"的信号，raw 没删完之前它必须还在，
        # 中途崩溃时留下"manifest 在但 raw 已删一部分"仍是无害状态（restore
        # 靠月库溯源判断，不看 raw 存不存在），反过来先删 manifest 会留下
        # 无主的 raw 文件
        p.unlink()
        deleted.append((month, var))
    return deleted
