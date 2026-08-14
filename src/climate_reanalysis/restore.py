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

from climate_reanalysis.archive import cold_path
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


def _resolve(path: Path, month: str, cold_dir: Path | None) -> Path:
    """热端文件不存在时回退到冷端——归档（``archive.py``）只搬文件本身、
    绝不改 manifest 内容，``path`` 记的仍是热端原路径，这里按 basename 去
    ``cold_path(cold_dir, month, ...)`` 找。两处都没有才报错；``cold_dir``
    为 ``None``（未配置归档）时行为与之前完全一致——只看热端。
    """
    if path.exists():
        return path
    if cold_dir is not None:
        cold = cold_path(Path(cold_dir), month, path.name)
        if cold.exists():
            return cold
        raise FileNotFoundError(
            f"原始文件缺失：热端 {path} 与冷端 {cold} 都没有"
        )
    raise FileNotFoundError(f"原始文件缺失：{path}（未配置 cold_dir，不做冷端回退）")


def load_raw(
    files: list[Path], var: str, month: str, cold_dir: Path | None = None
) -> tuple[np.ndarray, list[str]]:
    """读该产出变量的全部 CDS 文件，换算并对齐到契约网格与时间轴。

    网格与时间轴的校验**逐文件**做，不是只查循环里最后处理的那一个——
    多文件变量（si10 = u10 + v10）任何一个源文件跑偏，`np.hypot` 之类的
    纯位置逐元素运算都会静默合成出错误的值，而不是报错。

    时间轴校验的是**逐点相等**，不是数量相等：数量对但内容错位（缺一格、
    多一格但总数凑巧不变）如果只查数量会被放过，写进库里就是一次没有任何
    征兆的静默时间平移——这是下游直接读的库，这个位置的静默错误代价最高，
    比抛错严重得多，所以宁可算得贵一点也要挡在这里。

    第二个返回值是该产出变量涉及的全部原始文件**排序去重后**的 `expver`
    并集（ERA5 final=0001 / ERA5T 初步=0005 标记）——多文件变量（si10）
    的两个源文件各自记各自的，不要求互相一致，只取并集；文件里没有这个
    坐标（老数据/非该来源）时贡献空集，不报错。

    ``cold_dir`` 不为 ``None`` 时，逐文件在打开前先经 ``_resolve``：热端存在
    就用热端，热端缺失（已被 ``archive`` 搬走）则回退到冷端同名文件，两处
    都没有才报错——这是"月库丢了不必重下"自愈路径的落点。
    """
    arrays: dict[str, np.ndarray] = {}
    expvers: set[str] = set()
    expected_time = month_hours(month)
    paths = [Path(f) for f in files]
    for cds_var in VAR_SPECS[var].cds:
        # 按文件名匹配而非位置对应——位置对应只在 si10 恰好字母序一致时成立，
        # 将来加一个字母序不同的双源变量就会静默错配
        hit = [q for q in paths if q.stem == cds_var]
        if len(hit) != 1:
            raise ValueError(f"{var}: 找不到唯一的 {cds_var} 文件（{[q.name for q in paths]}）")
        ds = _open_one(_resolve(hit[0], month, cold_dir))
        if "expver" in ds.coords:
            # 读值必须在 ds.close() 之前，理由同下面 da.values 那行的注释
            expvers.update(str(v) for v in np.asarray(ds["expver"].values).tolist())
        if cds_var in ds.data_vars:
            name = cds_var
        else:
            # 真实 CDS netCDF 用的是 GRIB shortName（t/u/r 这种），不是 CDS 长
            # 变量名——上面这个分支在真实数据上永远不命中，每次都走到这里。
            # 唯一一个数据变量时按位置取用没有歧义；但文件里若混进了第二个数据
            # 变量，静默取 list(...)[0] 会不声不响地写错变量进库——必须报错
            # 点名，不能猜。
            data_vars = list(ds.data_vars)
            if len(data_vars) != 1:
                raise ValueError(
                    f"{var}/{cds_var}（{hit[0].name}）: 期望恰好一个数据变量"
                    f"（真实 CDS 文件用 GRIB shortName、非 {cds_var}），实际有 "
                    f"{len(data_vars)} 个：{data_vars}"
                )
            name = data_vars[0]
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
    return convert(var, arrays).astype("float32"), sorted(expvers)


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
    data, expver = load_raw([f["path"] for f in d["files"]], var, month, settings.cold_dir)
    write_variable(settings.store_dir, month, var, data,
                   {"manifest_sha256": sha, "completed_at": d["completed_at"],
                    "source": d["source"]["dataset"], "expver": expver})
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
