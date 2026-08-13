"""manifest：每（月 × 产出变量）一份，是唯一的就绪信号。

与 climate_download 的契约同构：**只在成功时写**——存在即代表该期该变量已完整
下载。restore 用 manifest 的 sha256 与月库里的溯源属性比对决定是否重建。

一个产出变量可能来自多个 CDS 变量（si10），它们同属这份 manifest 的 ``files[]``：
要么齐、要么不发布。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = 1


def manifest_path(manifest_dir: Path, month: str, var: str) -> Path:
    """生成 manifest 文件路径。"""
    return Path(manifest_dir) / f"{month}_{var}.manifest.json"


def _sha256_of(path: Path) -> str:
    """计算文件的 SHA256 哈希。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(
    manifest_dir: Path,
    month: str,
    var: str,
    files: list[Path],
    area: tuple[float, float, float, float],
    completed_at: dt.datetime | None = None,
) -> Path:
    """写一份 manifest。``files`` 为空时抛错——没有文件就不该有就绪信号。

    原子落地：写到临时文件后 replace 到目标位置，确保不会读到写了一半的 manifest。
    """
    files = [Path(f) for f in files]
    if not files:
        raise ValueError(f"{month}/{var}: files 为空，不得写 manifest")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": {"name": "era5", "dataset": "reanalysis-era5-single-levels"},
        "period": month,
        "variable": var,
        "area": list(area),
        "completed_at": (completed_at or dt.datetime.now(dt.UTC)).isoformat(),
        "files": [
            {
                "path": str(f),
                "size_bytes": f.stat().st_size,
                "sha256": _sha256_of(f),
            }
            for f in sorted(files)
        ],
    }
    out = manifest_path(manifest_dir, month, var)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)          # 原子落地：不会读到写了一半的 manifest
    return out


def read_manifest(path: Path) -> dict:
    """读取并解析 manifest JSON 文件。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def manifest_sha256(path: Path) -> str:
    """manifest 文件自身的 sha——溯源比对用的就是它。"""
    return _sha256_of(Path(path))


def iter_manifests(manifest_dir: Path) -> list[Path]:
    """列出目录内所有 manifest，按文件名排序。"""
    d = Path(manifest_dir)
    return sorted(d.glob("*.manifest.json")) if d.is_dir() else []
