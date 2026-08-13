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
from climate_reanalysis.download import download_many
from climate_reanalysis.logging_setup import configure, get_logger
from climate_reanalysis.migrate import stamp_legacy
from climate_reanalysis.period import ready_months
from climate_reanalysis.restore import scan_once
from climate_reanalysis.settings import load_settings
from climate_reanalysis.variables import VAR_SPECS

log = get_logger(__name__)


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


def cmd_list_variables(args) -> int:
    for name, spec in VAR_SPECS.items():
        print(f"{name:7s} {spec.units:10s} {spec.step_type:8s} <- {', '.join(spec.cds)}")
    return 0


def main(argv=None) -> int:
    configure()
    p = argparse.ArgumentParser(prog="climate_reanalysis")
    p.add_argument("--config", type=Path, default=Path("config/settings.yaml"))
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

    lv = sub.add_parser("list-variables", help="列出产出变量与其 CDS 来源")
    lv.set_defaults(func=cmd_list_variables)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
