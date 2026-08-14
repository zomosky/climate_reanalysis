"""CLI：download / restore / migrate / list-variables。

**只做参数→调用的翻译**，业务逻辑一律在 download/restore/migrate 模块里
（同 climate_zrread 的 CLI 子命令只做参数→QuerySpec 翻译的做法）。

``restore`` 是幂等的一趟扫描，直接可挂 cron/launchd（同 restorage 的 scan-once）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from climate_reanalysis.cds import make_client
from climate_reanalysis.download import (
    cleanup_account_failed_jobs,
    download_many,
    preview_account_failed_jobs,
)
from climate_reanalysis.logging_setup import configure, get_logger
from climate_reanalysis.migrate import stamp_legacy
from climate_reanalysis.period import ready_months
from climate_reanalysis.refresh import refresh
from climate_reanalysis.restore import scan_once
from climate_reanalysis.settings import load_settings
from climate_reanalysis.variables import VAR_SPECS

log = get_logger(__name__)

#: 约定的默认配置文件位置——只在用户没有显式传 --config 时才生效。
_DEFAULT_CONFIG = Path("config/settings.yaml")


def _resolve_config_path(explicit: Path | None) -> Path | None:
    """解析 --config：显式传入的路径原样交给 load_settings（不存在会报错，见其
    docstring）；没传则按约定路径，约定路径也不存在就交回 None（load_settings
    据此退回出厂默认值，不报错）。

    这条"约定路径缺失就静默用默认值、显式传入缺失就报错"的不对称必须在这里
    （而不是 load_settings 内部）判断——argparse 的 default 值一旦落进
    args.config，load_settings 单看这一个参数已经分不清"用户没传"和"用户
    传的正好等于约定路径"，必须由调用方在解析阶段就把这两种情况分开。
    """
    if explicit is not None:
        return explicit
    return _DEFAULT_CONFIG if _DEFAULT_CONFIG.exists() else None


def cmd_download(args) -> int:
    s = load_settings(args.config)
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    months = args.months or ready_months(args.start, today, s.publish_lag_days)
    variables = args.variables or list(VAR_SPECS)
    if not months:
        log.info("nothing_ready", start=args.start)
        return 0
    ok, failed = download_many(s, make_client(s), months, variables, today)
    log.info("download_done", ok=len(ok), failed=len(failed))
    return 1 if failed else 0


def cmd_restore(args) -> int:
    s = load_settings(args.config)
    done, skipped, failed = scan_once(s)
    log.info("restore_done", done=done, skipped=skipped, failed=failed)
    return 1 if failed else 0


def cmd_migrate(args) -> int:
    s = load_settings(args.config)
    n = stamp_legacy(s.store_dir, args.months, args.variables, args.tag)
    log.info("migrate_done", stamped=n)
    return 0


def cmd_cleanup_jobs(args) -> int:
    """账号级清理：删除该账号上全部失败/被拒 job，不限本批。危险操作——同账号
    可能正跑着别的下载任务（例如另一段历史回填），敲错账号/配置即刻执行、
    不可撤销。这是它跟 download 收尾自动做的清理（只清本轮）的区别，故独立
    成一个必须显式调用的子命令，不会被日常 cron 意外碰到。

    **dry-run 是默认行为**：不加 ``--yes`` 只预览将要删除什么（按状态分类的
    数量 + 若干示例 id），不做任何实际删除；必须显式加 ``--yes`` 才会真的
    执行删除。零防呆的账号级删除此前敲错即刻生效，没有任何找回办法。
    """
    s = load_settings(args.config)
    statuses = tuple(args.statuses) if args.statuses else ("failed", "rejected")
    client = make_client(s)
    if not args.yes:
        preview = preview_account_failed_jobs(client, statuses)
        total = 0
        for status, ids in preview.items():
            total += len(ids)
            log.info("cleanup_jobs_preview", status=status, count=len(ids),
                     sample=ids[:5])
        log.info("cleanup_jobs_dry_run", total=total,
                 hint="预览模式，未删除任何 job；加 --yes 才会真正执行")
        return 0
    n = cleanup_account_failed_jobs(client, statuses)
    log.info("cleanup_jobs_done", count=n)
    return 0


def cmd_refresh(args) -> int:
    """把过期的 ERA5T 月份（expver 仍含初步标记 "0005"、且已过换新窗口）的
    manifest 删掉，交给下一轮 download + restore 自然重下 final。危险操作
    （会触发真实重下，代价是排队时间和带宽）——形态照 ``cleanup-jobs`` 的
    范式，默认 dry-run，只有显式 ``--yes`` 才真正删除。

    ``refresh()`` 本身就同时承担"预览"和"执行"两种模式（``execute`` 参数），
    这里直接按 ``--yes`` 传下去，不必像 ``cleanup-jobs`` 那样另起一个专门的
    预览函数。
    """
    s = load_settings(args.config)
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    after_days = args.after_days if args.after_days is not None else s.refresh_after_days
    got = refresh(s.store_dir, s.manifest_dir, today, after_days, execute=args.yes)
    for month, var in got:
        log.info("refresh_item", month=month, var=var)
    if not args.yes:
        log.info("refresh_dry_run", total=len(got),
                 hint="预览模式，未删除任何 manifest；加 --yes 才会真正执行")
    else:
        log.info("refresh_done", count=len(got),
                 hint="manifest 已删除；需要再跑一次 download + restore 才会真正重下 final")
    return 0


def cmd_list_variables(args) -> int:
    for name, spec in VAR_SPECS.items():
        print(f"{name:7s} {spec.units:10s} {spec.step_type:8s} <- {', '.join(spec.cds)}")
    return 0


def main(argv=None) -> int:
    configure()
    p = argparse.ArgumentParser(prog="climate_reanalysis")
    p.add_argument("--config", type=Path, default=None, help=(
        "配置文件路径；不传则用约定的 config/settings.yaml（那个文件存在才用，"
        "不存在就退回出厂默认值，不报错——方便刚 clone 仓库、还没 cp 配置样例时"
        "先跑 list-variables 之类不依赖配置的子命令）。显式传入的路径必须存在，"
        "否则报错——避免 typo 静默用错配置、表现成'处理 0 个、退出码 0'"
    ))
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="按（月×产出变量）下载并写 manifest")
    d.add_argument("--start", required=True, help=(
        "起始月份 YYYYMM，必填——没有默认值是有意的：migrate 只写月库溯源、不产生 "
        "manifest，download 的跳过判据只看 manifest 在不在，两者互不知情。一个"
        "看起来无害的默认值（例如 200501）会让裸跑一次 download 把已迁移的历史"
        "月份当成'从未下载'重新发起真实 CDS 请求"
    ))
    d.add_argument("--months", nargs="*", help="显式指定月份；留空 = 起始到最后一个已齐全月")
    d.add_argument("--variables", nargs="*", help="留空 = 全部产出变量")
    d.add_argument("--today", help="覆盖“今天”（YYYY-MM-DD），用于测试就绪判据")
    d.set_defaults(func=cmd_download)

    r = sub.add_parser("restore", help="幂等扫描 manifest 并切进月库（可 cron）")
    r.set_defaults(func=cmd_restore)

    m = sub.add_parser("migrate", help="给既有月库的历史变量打 legacy 溯源")
    m.add_argument("--months", nargs="+", required=True, help="显式月份列表（YYYYMM ...）")
    m.add_argument("--variables", nargs="+", required=True)
    m.add_argument("--tag", default="legacy:solarcast-bake")
    m.set_defaults(func=cmd_migrate)

    cj = sub.add_parser("cleanup-jobs", help=(
        "账号级清理全部失败/被拒 job（危险，不限本批——独立入口，别挂进日常"
        "下载收尾，同账号可能正跑着别的批次；默认 dry-run 只预览，加 --yes 才真删）"
    ))
    cj.add_argument("--statuses", nargs="*", help="留空 = failed rejected")
    cj.add_argument("--yes", action="store_true", help=(
        "真正执行删除；不加则只预览将要删除什么（数量+按状态分类+示例 id），"
        "不做任何实际删除——默认行为，账号级清理不可撤销、敲错即刻生效"
    ))
    cj.set_defaults(func=cmd_cleanup_jobs)

    rf = sub.add_parser("refresh", help=(
        "把过期 ERA5T 月份（expver 仍含初步标记）的 manifest 删掉，让下一轮 "
        "download + restore 自然重下 final（危险，会触发真实重下；默认 "
        "dry-run 只预览，加 --yes 才真删）"
    ))
    rf.add_argument("--today", help="覆盖“今天”（YYYY-MM-DD），用于测试换新窗口判据")
    rf.add_argument("--after-days", type=int, default=None,
                    help="留空 = 配置里的 refresh_after_days（出厂 100）")
    rf.add_argument("--yes", action="store_true", help=(
        "真正删除 manifest；不加则只打印将要刷新的 (月, 变量) 清单，不删任何"
        "文件——默认行为，删 manifest 会触发真实重下，代价是排队时间和带宽"
    ))
    rf.set_defaults(func=cmd_refresh)

    lv = sub.add_parser("list-variables", help="列出产出变量与其 CDS 来源")
    lv.set_defaults(func=cmd_list_variables)

    args = p.parse_args(argv)
    args.config = _resolve_config_path(args.config)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
