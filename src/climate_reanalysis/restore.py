"""单元 B：scan-once 幂等地把 raw 切进月度国网库。

**幂等靠产出自述**（spec §4）：逐 manifest 读目标月库的 ``zrread_provenance[var]``，
manifest 的 sha 一致就跳过，不一致或缺失就只重建**那一个变量**。状态就在产出里，
没有与现实脱节的路径——外部账本才会。

**时间轴保持 ERA5 原生整点，不做任何中点搬移**：累积量标签在区间右边界、瞬时量为
该时刻值。zrread 把同一件对齐放在查询时（由 ``zrread_flux`` 触发）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr

from climate_reanalysis.logging_setup import get_logger
from climate_reanalysis.manifest import (
    iter_manifests,
    manifest_sha256,
    read_manifest,
)
from climate_reanalysis.period import month_hours
from climate_reanalysis.store import (
    TARGET_LAT,
    TARGET_LON,
    read_provenance,
    write_variable,
)
from climate_reanalysis.variables import VAR_SPECS, convert

log = get_logger(__name__)


def _open_one(path: Path) -> xr.Dataset:
    ds = xr.open_dataset(path)
    tname = "valid_time" if "valid_time" in ds.coords else "time"
    return ds.rename({tname: "time"})


def load_raw(files: list[Path], var: str, month: str) -> np.ndarray:
    """读该产出变量的全部 CDS 文件，换算并对齐到契约网格与时间轴。

    网格与时间轴的校验**逐文件**做，不是只查循环里最后处理的那一个——
    多文件变量（si10 = u10 + v10）任何一个源文件跑偏，`np.hypot` 之类的
    纯位置逐元素运算都会静默合成出错误的值，而不是报错。

    时间轴校验的是**逐点相等**，不是数量相等：数量对但内容错位（缺一格、
    多一格但总数凑巧不变）如果只查数量会被放过，写进库里就是一次没有任何
    征兆的静默时间平移——这是下游直接读的库，这个位置的静默错误代价最高，
    比抛错严重得多，所以宁可算得贵一点也要挡在这里。
    """
    arrays: dict[str, np.ndarray] = {}
    expected_time = month_hours(month)
    paths = [Path(f) for f in files]
    for cds_var in VAR_SPECS[var].cds:
        # 按文件名匹配而非位置对应——位置对应只在 si10 恰好字母序一致时成立，
        # 将来加一个字母序不同的双源变量就会静默错配
        hit = [q for q in paths if q.stem == cds_var]
        if len(hit) != 1:
            raise ValueError(f"{var}: 找不到唯一的 {cds_var} 文件（{[q.name for q in paths]}）")
        ds = _open_one(hit[0])
        name = cds_var if cds_var in ds.data_vars else list(ds.data_vars)[0]
        da = ds[name].sortby("latitude").sortby("longitude").sortby("time")
        lat = da["latitude"].values
        lon = da["longitude"].values
        time = da["time"].values
        values = da.values          # 取数据必须在 ds.close() 之前——关闭后端后
        ds.close()                  # da 的惰性读取会失效
        if not np.allclose(lat, TARGET_LAT) or not np.allclose(lon, TARGET_LON):
            raise ValueError(f"{var}/{cds_var}: 原始网格与契约不符")
        if time.shape != expected_time.shape or not np.array_equal(time, expected_time):
            raise ValueError(
                f"{month}/{var}/{cds_var}: 时间轴与契约不符"
                f"（{time.size} 步 vs 期望 {expected_time.size} 步，或时刻错位）"
            )
        arrays[cds_var] = values
    return convert(var, arrays).astype("float32")


def restore_one(settings, manifest_file: Path) -> str:
    """处理一份 manifest。返回 ``"done"``（已重建）或 ``"skip"``（溯源一致）。"""
    manifest_file = Path(manifest_file)
    d = read_manifest(manifest_file)
    month, var = d["period"], d["variable"]
    sha = manifest_sha256(manifest_file)
    prov = read_provenance(settings.store_dir, month).get(var)
    if prov and prov.get("manifest_sha256") == sha:
        return "skip"
    # 形状/时间轴的校验已经下沉到 load_raw（逐文件核对，比这里事后再查一次
    # data.shape[0] 更严格——它连"数量对但内容错位"都会挡住），这里不必重复。
    data = load_raw([f["path"] for f in d["files"]], var, month)
    write_variable(settings.store_dir, month, var, data,
                   {"manifest_sha256": sha, "completed_at": d["completed_at"],
                    "source": d["source"]["dataset"]})
    log.info("restored", month=month, var=var)
    return "done"


def scan_once(settings) -> tuple[int, int, int]:
    """一趟幂等扫描。返回 (done, skipped, failed)。"""
    done = skipped = failed = 0
    for mf in iter_manifests(settings.manifest_dir):
        try:
            if restore_one(settings, mf) == "done":
                done += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001 - 单份失败不中断整趟
            log.error("restore_failed", manifest=str(mf), error=str(exc))
            failed += 1
    return done, skipped, failed
