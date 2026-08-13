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


def load_raw(files: list[Path], var: str) -> np.ndarray:
    """读该产出变量的全部 CDS 文件，换算并对齐到契约网格与时间轴。"""
    arrays: dict[str, np.ndarray] = {}
    ref_lat = ref_lon = None
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
        arrays[cds_var] = da.values
        ref_lat = da["latitude"].values
        ref_lon = da["longitude"].values
        ds.close()
    if not np.allclose(ref_lat, TARGET_LAT) or not np.allclose(ref_lon, TARGET_LON):
        raise ValueError(f"{var}: 原始网格与契约不符")
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
    data = load_raw([f["path"] for f in d["files"]], var)
    expect = month_hours(month).size
    if data.shape[0] != expect:
        raise ValueError(f"{month}/{var}: 时间步数 {data.shape[0]} != {expect}")
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
