"""单元 C：给既有月库的历史变量打 legacy 溯源，使 restore 认为它们已就位。

依据是实测：ARCO 时序端点与 single-levels 对同一区域同一时段逐值相同
（``array_equal`` = True），故历史数据可复用、不必重下。

**``fdir`` 例外**：历史段是 solarcast 路径的产物，经过 ``bhi = min(fdir/3600, ghi)``
截断再反解，与 CDS 原生值严格讲不同源。故它单独标 ``(clipped)``，使差异在数据里
可见、将来可选择重下。

**字段对齐**：这里复用 ``store.set_provenance``——与 ``restore.restore_one``/
``store.write_variable`` 写入/比对溯源时用的是同一个函数、同一个
``zrread_provenance[var]`` 位置。legacy 条目不含 ``manifest_sha256``（本就没有
manifest），因此不会被误判成任何真实 manifest 的产物；也正因为没有 manifest，
``restore.scan_once`` 只会遍历 manifest 目录里存在的文件，永远不会主动碰到这些
历史月份——除非将来真的为它们生成了 manifest（例如显式重新下载某个历史月），
届时两者 sha 天然不相等，restore 会用新数据覆盖 legacy 占位，这是有意的行为
（迁移只负责"标记已有"，不负责"锁死不可覆盖"）。
"""

from __future__ import annotations

from pathlib import Path

from climate_reanalysis.logging_setup import get_logger
from climate_reanalysis.store import set_provenance, store_path

log = get_logger(__name__)

#: 历史段与原生值不同源的变量（见模块 docstring）
_CLIPPED = {"fdir"}


def stamp_legacy(store_dir: Path, months: list[str], variables: list[str],
                 tag: str = "legacy:solarcast-bake") -> int:
    """给指定月份的指定变量打 legacy 溯源。返回打标的条数。

    月库不存在则跳过该月（不代为创建）——迁移只标记已有数据，不该凭空建出
    一个全 NaN 却"看似已就位"的库。
    """
    n = 0
    for month in months:
        if not store_path(store_dir, month).exists():
            log.warning("skip_missing_store", month=month)
            continue
        for var in variables:
            src = f"{tag}(clipped)" if var in _CLIPPED else tag
            set_provenance(store_dir, month, var, {"source": src})
            n += 1
    return n
