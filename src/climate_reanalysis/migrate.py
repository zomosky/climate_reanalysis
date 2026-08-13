"""单元 C：给既有月库的历史变量打 legacy 溯源，使 restore 认为它们已就位。

依据是实测：ARCO 时序端点与 single-levels 对同一区域同一时段逐值相同
（``array_equal`` = True），故历史数据可复用、不必重下。

**``fdir`` 例外**：历史段是 solarcast 路径的产物，经过 ``bhi = min(fdir/3600, ghi)``
截断再反解，与 CDS 原生值严格讲不同源。故它单独标 ``(clipped)``，使差异在数据里
可见、将来可选择重下。

**只补缺，不改写已有真实溯源**：若某（月×变量）已经有 ``manifest_sha256``——说明它
是被这条管线正常下载 + restore 切割过的，不是历史遗留——``stamp_legacy`` 会跳过并
告警，不会覆盖。这不是多此一举的保守：迁移的月份范围一旦与已经正常下载过的月份
重叠（人工传参很容易传错），无条件覆盖会把真实的 ``manifest_sha256`` 换成 legacy
标记；此时若对应的 raw 文件已经按 README 建议清理掉，后续 restore 想重建这个
变量都做不到——数据没丢，但"这份数据是怎么来的"这条线索丢了，且不会有任何报错。

**字段对齐**：写入走 ``store.set_provenance``——与 ``restore.restore_one``/
``store.write_variable`` 写入/比对溯源时用的是同一个函数、同一个
``zrread_provenance[var]`` 位置。legacy 条目不含 ``manifest_sha256``（本就没有
manifest），因此不会被误判成任何真实 manifest 的产物；也正因为没有 manifest，
``restore.scan_once`` 只会遍历 manifest 目录里存在的文件，永远不会主动碰到这些
历史月份——除非将来真的为它们生成了 manifest（例如显式重新下载某个历史月），
届时两者 sha 天然不相等，restore 会用新数据覆盖 legacy 占位；而这里的"跳过已有
真实溯源"堵住的是另一条路径——**迁移本身**误把已下载月份的真实溯源覆写掉。
"""

from __future__ import annotations

from pathlib import Path

from climate_reanalysis.logging_setup import get_logger
from climate_reanalysis.store import has_data, read_provenance, set_provenance, store_path
from climate_reanalysis.variables import VAR_SPECS

log = get_logger(__name__)

#: 历史段与原生值不同源的变量（见模块 docstring）
_CLIPPED = {"fdir"}


def stamp_legacy(store_dir: Path, months: list[str], variables: list[str],
                 tag: str = "legacy:solarcast-bake") -> int:
    """给指定月份的指定变量打 legacy 溯源。返回打标的条数（跳过的不计入）。

    ``variables`` 必须都是 ``VAR_SPECS`` 里的产出变量名——拼错的变量名不会
    报错就默默进不了任何一个月库（``store_path`` 判存在、``read_provenance``
    的 dict 查找都不会因为 key 不存在而报错），会在 ``zrread_provenance`` 里
    悄悄写出一个没有任何数据数组、也不会被任何代码读到的孤立 key，且没有
    任何提示——必须在动手之前就拦下来。

    月库不存在则跳过该月（不代为创建）——迁移只标记已有数据，不该凭空建出
    一个全 NaN 却"看似已就位"的库。变量已有真实 ``manifest_sha256`` 溯源则跳过
    该变量并告警（见模块 docstring）——只补缺，不改写已有真实数据。变量数组
    本身没有任何实际数据（真实 legacy 库实测：``ssrdc`` 是全 NaN，从没真正
    落过数据）同样跳过并告警——打上 legacy 标记会让 restore 认为它已经就位，
    此后永远不会被真正下载补上。
    """
    unknown = sorted(set(variables) - set(VAR_SPECS))
    if unknown:
        raise ValueError(
            f"未知产出变量（不在 VAR_SPECS 里，是否拼错了）：{unknown}；"
            f"可用变量：{sorted(VAR_SPECS)}"
        )
    n = 0
    for month in months:
        if not store_path(store_dir, month).exists():
            log.warning("skip_missing_store", month=month)
            continue
        existing = read_provenance(store_dir, month)
        for var in variables:
            if "manifest_sha256" in (existing.get(var) or {}):
                log.warning("skip_real_provenance", month=month, var=var)
                continue
            if not has_data(store_dir, month, var):
                log.warning("skip_no_data", month=month, var=var)
                continue
            src = f"{tag}(clipped)" if var in _CLIPPED else tag
            set_provenance(store_dir, month, var, {"source": src})
            n += 1
    return n
