"""一次性迁移：把扁平的月度国网库挪进按年分层的子目录。

历史遗留：``store_path()`` 在改按年分层之前是扁平布局
``<store_dir>/<YYYYMM>_era5.zarr``；改动之后新建的库都已经按新布局落盘
（``<store_dir>/<YYYY>/<YYYYMM>_era5.zarr``，见 ``store.py``），但已有的
历史月库还堆在旧的扁平位置，需要挪一次——这份脚本就是干这个的，只搬一次，
不是常驻管线的一部分。

同一块盘内挪目录用 ``os.rename``——秒级操作，不是逐文件拷贝再删除。
``archive.py`` 的冷存归档要跨盘搬到冷盘，才需要那一套"拷贝到临时名 → 校验
sha256 → 原子改名 → 确认后删源"的重流程；这里源和目标在同一个 ``store_dir``
下，`os.rename` 本身就是同目录树内的原子操作，不需要那么重。

同 ``archive``/``refresh``/``cleanup-jobs`` 的既有范式：**默认 dry-run，只有
显式 ``--yes`` 才真正执行**——搬动一批历史月库之前，必须先看清楚完整清单。

幂等：已经在按年目录下的库不会被当作候选（只扫 ``store_dir`` 的直接子项，
按年分层之后旧的扁平文件名不会再出现），重跑天然是空操作。目标位置已经
存在（理论上不应该发生，但保守起见仍要防）——报错点名并跳过，不覆盖，
交给人工核实是不是重复/冲突；不阻塞其余月库继续搬。
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path

from climate_reanalysis.logging_setup import configure, get_logger
from climate_reanalysis.store import store_path

log = get_logger(__name__)

#: 旧扁平布局的落盘文件名——六位月份 token + store_path 的固定后缀。
_FLAT_NAME_RE = re.compile(r"^(\d{6})_era5\.zarr$")


@dataclass(frozen=True)
class MigrationPlan:
    """一个月库的搬运计划：``month`` 由旧扁平文件名反推，``dst`` 直接调用
    ``store_path()`` 算出——与 ``store.py`` 的落盘契约保持唯一真值，不在这里
    重新拼一遍路径。"""

    month: str
    src: Path
    dst: Path


def _iter_flat_stores(store_dir: Path) -> list[Path]:
    """列出 ``store_dir`` 直接子项里旧扁平命名的月库（按文件名排序）。

    只看直接子项——按年分层之后，已迁移的月库是 ``store_dir/<YYYY>/
    <YYYYMM>_era5.zarr``，年份目录本身不匹配扁平命名正则，天然不会被
    当作候选，这正是幂等的来源。
    """
    d = Path(store_dir)
    if not d.is_dir():
        return []
    out = [p for p in d.iterdir() if p.is_dir() and _FLAT_NAME_RE.match(p.name)]
    return sorted(out, key=lambda p: p.name)


def plan_migration(store_dir: Path) -> list[MigrationPlan]:
    """扫描 ``store_dir``，返回需要搬运的月库计划。"""
    store_dir = Path(store_dir)
    plans = []
    for p in _iter_flat_stores(store_dir):
        month = p.name[:6]
        plans.append(MigrationPlan(month=month, src=p, dst=store_path(store_dir, month)))
    return plans


def migrate(
    store_dir: Path, execute: bool = False
) -> tuple[list[MigrationPlan], list[MigrationPlan]]:
    """把扁平月库挪进按年分层的子目录。返回 ``(搬运计划, 因目标已存在被跳过的
    计划)``——两者都记入返回值，调用方（CLI）据此决定退出码。

    ``execute=False``（默认）只规划、打日志，不碰任何文件；``execute=True``
    才真的 ``os.rename``。目标已存在的一律跳过、不覆盖，无论 dry-run 还是
    真跑都会被识别并点名。
    """
    plans = plan_migration(store_dir)
    todo: list[MigrationPlan] = []
    conflicts: list[MigrationPlan] = []
    for plan in plans:
        if plan.dst.exists():
            conflicts.append(plan)
            log.error(
                "migrate_year_layout_conflict", month=plan.month,
                src=str(plan.src), dst=str(plan.dst),
                hint="目标已存在，跳过——不会覆盖；需要人工核实是不是重复/冲突",
            )
            continue
        todo.append(plan)
        if not execute:
            log.info(
                "migrate_year_layout_would_move", month=plan.month,
                src=str(plan.src), dst=str(plan.dst),
            )
            continue
        plan.dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(plan.src, plan.dst)
        log.info(
            "migrate_year_layout_moved", month=plan.month,
            src=str(plan.src), dst=str(plan.dst),
        )
    return todo, conflicts


def main(argv: list[str] | None = None) -> int:
    configure()
    p = argparse.ArgumentParser(
        description=(
            "一次性迁移：把 <store_dir>/<YYYYMM>_era5.zarr（扁平）挪进 "
            "<store_dir>/<YYYY>/<YYYYMM>_era5.zarr（按年分层）"
        )
    )
    p.add_argument(
        "--store-dir", type=Path, required=True,
        help="月度国网库根目录（即 settings.yaml 里的 store_dir，例如 "
             "/Volumes/TUF/climate/era5/monthly）",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="真正执行搬运；不加则只预览将要搬运的月库清单，不移动任何文件"
             "（默认行为，同 archive/refresh/cleanup-jobs 的范式）",
    )
    args = p.parse_args(argv)

    todo, conflicts = migrate(args.store_dir, execute=args.yes)
    if not args.yes:
        log.info(
            "migrate_year_layout_dry_run", would_move=len(todo),
            conflicts=len(conflicts),
            hint="预览模式，未移动任何文件；确认清单无误后加 --yes 才会真正执行",
        )
    else:
        log.info("migrate_year_layout_done", moved=len(todo), conflicts=len(conflicts))
    return 1 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
