"""冷存归档：把已固化到月库的原始 netCDF 从热盘搬到冷盘，热盘省地方、冷盘保留
自愈能力。

判据沿用兄弟项目 ``forecast-coldmove.sh`` 的语义——**下游产品已固化才允许
搬**（这里的"固化"= 该 (月, 变量) 的月库溯源 ``manifest_sha256`` 与该 manifest
当前的 sha 相等，即 restore 确已完成且未过期）——但校验做得更严：manifest 里
本来就记了每个原始文件的 sha256，这里逐文件比对，而不是像那份脚本一样只数
文件个数。

**先搬后删、验完再删，顺序即安全性**（不许调换）：

1. 拷贝到冷端的临时名（同目录，带 pid，同 ``manifest.py`` 写 manifest 的做法：
   避免两个并发的归档进程互相踩踏出半成品文件）。
2. 算冷端副本的 sha256，与 manifest 里记录的那个比对；不符 → 删临时文件、
   跳过整份 manifest（保守：一份 manifest 可能对应多个原始文件，如 si10 的
   u10/v10，任何一个校验不过就整份都不动，不做部分搬运）、告警。**绝不删
   热端**。
3. 全部文件校验通过后，才 ``os.replace`` 临时名 → 冷端正式名（同目录内
   原子操作）。
4. 确认冷端正式名就位（存在 + 内容 sha 仍正确）之后，才删热端。

任何一步失败都不许删热端——热端在归档这一刻仍是唯一已确认完整的副本，这条
顺序反了就是不可逆的数据丢失（本项目前身出过一次类似事故：一个"完整性判据"
写宽了，损坏的月库被静默整月重建、清空真实数据）。

**绝不修改 manifest 文件的内容**：manifest 的 sha256 是溯源比对键，改它等于
宣布全部月份需要重建。冷端位置因此**不写进 manifest**——``restore.load_raw``
在热端找不到文件时，按 ``cold_path(cold_dir, month, basename)`` 去冷端找，
实现自愈；两处都没有才报错。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from climate_reanalysis.logging_setup import get_logger
from climate_reanalysis.manifest import iter_manifests, manifest_sha256, read_manifest
from climate_reanalysis.store import read_provenance

log = get_logger(__name__)

#: 冷盘挂载点必须放着这个文件才允许归档写入——见 ``_require_cold_sentinel``。
COLD_SENTINEL_NAME = ".climate_reanalysis_cold"


class ColdDirNotMountedError(RuntimeError):
    """冷盘目录未通过挂载确认（不存在 / 不是目录 / 缺哨兵文件）。

    挂载点在没挂载时，宿主机上通常表现为一个**存在、可写的空目录**（挂载失败
    本身不报错，只是没有内容）——单纯判断"目录是否存在"挡不住这种情况：会在
    冷盘没挂上的时候，把整棵冷端目录树建在热盘（或根盘）上，拷贝、校验（同
    一块盘，天然全部一致）、然后删掉热端；等真正的冷盘挂上，这份"冷端副本"
    被挂载点遮蔽，restore 回退时两边都找不到——热端已删、冷端不可用，数据
    就丢了。哨兵文件是运维在**确认挂载之后**手动放的信号，纯代码判断不出
    "挂载"这件事，只能靠这个信号替代。
    """


def _require_cold_sentinel(cold_dir: Path) -> None:
    """归档写入前的强制前置条件：``cold_dir`` 必须已经存在、是目录、且其下
    有哨兵文件 ``COLD_SENTINEL_NAME``。只允许在它下面建月份子目录（
    ``cold_path(...).parent.mkdir(parents=True, ...)``），绝不允许把
    ``cold_dir`` 本身建出来——这里提前拒绝就是为了保证那一步的 ``mkdir``
    永远只会创建月份子目录。
    """
    if not cold_dir.is_dir():
        raise ColdDirNotMountedError(
            f"冷盘目录不存在或不是目录：{cold_dir}——归档拒绝执行。这通常意味着"
            "冷盘还没挂载（未挂载时该路径可能干脆不存在），也可能是 cold_dir "
            "配置写错了。确认冷盘已挂载、路径正确后重试"
        )
    sentinel = cold_dir / COLD_SENTINEL_NAME
    if not sentinel.exists():
        raise ColdDirNotMountedError(
            f"冷盘目录 {cold_dir} 下缺少哨兵文件 {COLD_SENTINEL_NAME}——归档拒绝"
            "执行。挂载点在未挂载时通常仍表现为一个存在的空目录，只判断目录"
            "存在挡不住误把数据搬到热盘上再删掉热端这个风险。确认冷盘确已"
            f"挂载后执行：touch {sentinel}"
        )


def cold_path(cold_dir: Path, month: str, name: str) -> Path:
    """冷端某个原始文件应落盘的位置——布局与 ``raw_dir/<month>/<name>`` 同构，
    只是根换成冷盘。这个函数是归档与 restore 冷端回退共用的唯一真值，两边都
    不该各自拼路径。
    """
    return Path(cold_dir) / month / name


def _sha256_of(path: Path) -> str:
    """计算文件的 SHA256 哈希。与 ``manifest._sha256_of`` 逻辑相同但刻意独立
    成一份——归档校验的是"冷端副本算出来的 sha"，测试需要单独打桩这一步来
    模拟"搬运途中损坏"，不能跟 manifest 模块共用同一个引用点。
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archivable(manifest_dir: Path, store_dir: Path) -> list[Path]:
    """返回可以搬的 manifest 路径：该 (月, 变量) 在月库里的溯源
    ``manifest_sha256`` 与该 manifest **当前**的 sha 相等（即 restore 确已
    完成且未过期）。不相等（月库溯源落后于当前 manifest）或没有溯源（还没
    restore 过、或月库本身还不存在）一律不搬——搬走一份下游还没真正读过、
    或已经过期的原始文件，一旦月库这边也出问题就没有退路了。
    """
    out: list[Path] = []
    for mf in iter_manifests(manifest_dir):
        d = read_manifest(mf)
        month, var = d["period"], d["variable"]
        prov = read_provenance(store_dir, month).get(var)
        if not prov or prov.get("manifest_sha256") != manifest_sha256(mf):
            continue
        out.append(mf)
    return out


def _archive_one(cold_dir: Path, month: str, files: list[dict]) -> bool:
    """搬一份 manifest 涉及的全部原始文件。全部校验通过才动热端；任意一步
    失败都返回 False、不删任何热端文件（调用方据此计入"跳过"）。

    一份 manifest 可能对应多个原始文件（si10 = u10 + v10 两个源文件）——
    这里把"全部文件都验证通过"当作这份 manifest 能否归档的原子单位：任何
    一个文件的冷端副本 sha 不符，整份 manifest 都不动，不做部分搬运（否则
    这个变量的原始数据会一半在热端一半在冷端，restore 冷端回退是按文件名
    单独查找不会错，但排查/运维心智负担不值得为一次性搬运的一致性让路）。
    """
    created_tmp: list[Path] = []
    try:
        planned: list[tuple[Path, Path, str]] = []   # (tmp, cold, sha)
        for f in files:
            hot = Path(f["path"])
            cold = cold_path(cold_dir, month, hot.name)
            cold.parent.mkdir(parents=True, exist_ok=True)
            # tmp 名带 pid，同 manifest.py 写 manifest 的做法——避免两个并发
            # 归档进程互相踩踏出半成品文件
            tmp = cold.with_name(f"{cold.name}.{os.getpid()}.tmp")
            shutil.copy2(hot, tmp)
            created_tmp.append(tmp)
            got = _sha256_of(tmp)
            if got != f["sha256"]:
                log.warning(
                    "archive_sha_mismatch", month=month, file=str(hot),
                    expected=f["sha256"], got=got,
                    hint="冷端副本损坏或搬运中途出错——已跳过，热端未动",
                )
                return False
            planned.append((tmp, cold, f["sha256"]))

        # 全部校验通过——才原子改名到冷端正式名
        for tmp, cold, _sha in planned:
            os.replace(tmp, cold)
        created_tmp = []   # 已经全部改名，没有残留 tmp 需要在 finally 里清理

        # 确认冷端正式名就位（存在 + 内容 sha 仍正确）之后，才删热端
        for _tmp, cold, sha in planned:
            if not cold.exists() or _sha256_of(cold) != sha:
                log.error(
                    "archive_cold_verify_failed", month=month, cold=str(cold),
                    hint="冷端正式名校验未通过——热端保留，不做任何删除",
                )
                return False

        for f in files:
            Path(f["path"]).unlink()
        return True
    finally:
        # 校验失败/异常路径下清理已产生但还没改名的临时文件；一旦改名成功
        # created_tmp 已清空，这里不会误删正式名
        for t in created_tmp:
            t.unlink(missing_ok=True)


def archive(
    manifest_dir: Path,
    store_dir: Path,
    cold_dir: Path | None,
    execute: bool = False,
) -> tuple[int, int]:
    """把已固化（``archivable``）的原始文件搬到冷盘。返回
    ``(搬走的文件数, 跳过的 manifest 数)``。

    ``execute=False``（默认）只统计"将搬"多少文件，不碰任何文件——归档会
    删热端，代价是这批原始文件此后只能靠冷端自愈，必须先看清楚数量再决定。

    ``cold_dir=None`` 一律拒绝执行（哪怕只是预览）并抛错：出厂就是未配置，
    不给任何看似无害的默认路径，本机/测试环境通常没有那块盘。

    ``execute=True`` 时额外要求 ``cold_dir`` 通过 ``_require_cold_sentinel``
    的挂载确认——预览不写盘，不强制要求；真正要写盘（会在冷盘没挂载时把
    目录树建到热盘上）之前必须确认过。
    """
    if cold_dir is None:
        raise ValueError(
            "cold_dir 未配置——归档拒绝执行。请在 settings.yaml 里显式指定"
            "冷盘归档根目录（服务器示例见 README「冷存归档」一节）"
        )
    cold_dir = Path(cold_dir)
    if execute:
        _require_cold_sentinel(cold_dir)

    candidates = archivable(manifest_dir, store_dir)
    moved = 0
    skipped = 0
    for mf in candidates:
        d = read_manifest(mf)
        files = d["files"]
        if not execute:
            moved += len(files)
            continue
        if _archive_one(cold_dir, d["period"], files):
            moved += len(files)
            log.info("archived", month=d["period"], var=d["variable"], files=len(files))
        else:
            skipped += 1
    return moved, skipped
