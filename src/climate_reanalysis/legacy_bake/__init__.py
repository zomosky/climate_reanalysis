"""省库 → zrread 月度国网库的烘焙作业。

省库（``provinces_multiyear/<adcode>.zarr``）是为光伏发电量测算切的：按空间
分片、20 年一个时间块、变量名与单位随 pvlib、所有量搬到区间中点 :30。
本包把它烘成 zrread 的观测源契约——按时间分片的月度国网库、ERA5 原生整点轴、
ECMWF 变量名与 SI 单位。

**一次性历史导入**：这条路径只用于把 solarcast 早年下载、按省切好的 20 年
tile 一次性烘成月度国网库，本项目的持续积累改为逐月直接从 CDS 下载
（``download``/``restore`` 两个单元）之后就不再需要它——保留是因为它属于
数据处理，不该分散在别的项目里；不是长期运行的路径。CLI 入口见
``climate_reanalysis.cli`` 的 ``bake-legacy`` 子命令，用法见 README「历史导入
（一次性烘焙）」一节。

设计见 climate_zrread/docs/superpowers/specs/2026-08-11-era5-reanalysis-source-design.md
（原 solarcast 项目内的设计文档路径，供追溯历史背景）。
"""

from climate_reanalysis.legacy_bake.deconv import from_interval_centre
from climate_reanalysis.legacy_bake.grid import (
    TARGET_LAT,
    TARGET_LON,
    Ledger,
    coverage_from_tiles,
    shard_slices,
)

__all__ = [
    "from_interval_centre",
    "TARGET_LAT",
    "TARGET_LON",
    "Ledger",
    "coverage_from_tiles",
    "shard_slices",
]
