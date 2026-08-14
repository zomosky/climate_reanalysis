"""冷存归档：先搬后删，验完再删。"""

from __future__ import annotations

from pathlib import Path

import pytest

from climate_reanalysis.archive import (
    COLD_SENTINEL_NAME,
    ColdDirNotMountedError,
    archivable,
    archive,
    cold_path,
)
from climate_reanalysis.manifest import manifest_sha256, manifest_path, write_manifest
from climate_reanalysis.store import ensure_store, set_provenance

AREA = (55.0, 70.0, 15.0, 140.0)


def _mount(cd: Path) -> None:
    """标记冷盘"已挂载"：真实运维流程是确认挂载后手动 touch 哨兵文件，测试里
    直接建目录 + 放哨兵模拟这个已确认状态。"""
    cd.mkdir(parents=True, exist_ok=True)
    (cd / COLD_SENTINEL_NAME).touch()


def _setup(
    tmp_path: Path, *, matching: bool = True, mount_cold: bool = False
) -> tuple[Path, Path, Path, Path]:
    md, sd, cd, rd = (tmp_path / n for n in ("m", "s", "cold", "raw"))
    rd.mkdir(parents=True)
    raw = rd / "2m_temperature.nc"
    raw.write_bytes(b"payload" * 100)
    write_manifest(md, "202601", "t2m", [raw], AREA)
    ensure_store(sd, "202601")
    sha = manifest_sha256(manifest_path(md, "202601", "t2m")) if matching else "stale-sha"
    set_provenance(sd, "202601", "t2m", {"manifest_sha256": sha})
    if mount_cold:
        _mount(cd)
    return md, sd, cd, raw


def test_archivable_requires_provenance_match(tmp_path: Path):
    md, sd, _, _ = _setup(tmp_path, matching=False)
    assert archivable(md, sd) == []


def test_archivable_when_restore_is_done(tmp_path: Path):
    md, sd, _, _ = _setup(tmp_path)
    assert archivable(md, sd) == [manifest_path(md, "202601", "t2m")]


def test_dry_run_moves_nothing(tmp_path: Path):
    md, sd, cd, raw = _setup(tmp_path)
    moved, _ = archive(md, sd, cd)          # execute 默认 False
    assert moved == 1                        # 报告「将搬 1 个」
    assert raw.exists() and not cd.exists()  # 但一个都没动


def test_execute_moves_verifies_then_deletes_hot(tmp_path: Path):
    md, sd, cd, raw = _setup(tmp_path, mount_cold=True)
    body = raw.read_bytes()
    archive(md, sd, cd, execute=True)
    cold = cold_path(cd, "202601", "2m_temperature.nc")
    assert cold.read_bytes() == body      # 冷端内容一致
    assert not raw.exists()                # 热端已清
    assert not list(cd.rglob("*.tmp"))     # 无临时文件残留


def test_sha_mismatch_keeps_hot_file(tmp_path: Path):
    """搬运途中损坏——热端是最后一份可用副本，绝不能删。"""
    md, sd, cd, raw = _setup(tmp_path, mount_cold=True)
    import climate_reanalysis.archive as mod
    monkey = lambda p: "deadbeef"          # 冷端副本算出来的 sha 永远不符
    orig = mod._sha256_of
    mod._sha256_of = monkey
    try:
        archive(md, sd, cd, execute=True)
    finally:
        mod._sha256_of = orig
    assert raw.exists()                    # 热端还在
    assert not cold_path(cd, "202601", "2m_temperature.nc").exists()


def test_unconfigured_cold_dir_refuses(tmp_path: Path):
    md, sd, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError):
        archive(md, sd, None, execute=True)


def test_execute_refuses_when_cold_dir_does_not_exist(tmp_path: Path):
    """挂载点未挂载时通常压根不存在——光判断"配了 cold_dir"挡不住，必须真的
    存在。冷盘没挂就执行会把整棵目录树建在热盘/根盘上，见 archive.py 顶部
    docstring 的事故描述。"""
    md, sd, cd, raw = _setup(tmp_path)   # cd 从未创建
    with pytest.raises(ColdDirNotMountedError):
        archive(md, sd, cd, execute=True)
    assert raw.exists()          # 热端分毫未动
    assert not cd.exists()       # 也没有被顺手建出来


def test_execute_refuses_when_sentinel_missing(tmp_path: Path):
    """挂载点未挂载时，在宿主机上通常表现为一个存在、可写的空目录（挂载失败
    不报错，只是没有内容）——单纯"目录存在"挡不住这种情况，必须要哨兵文件。"""
    md, sd, cd, raw = _setup(tmp_path)
    cd.mkdir(parents=True)       # 目录"存在"，但没放哨兵——模拟未挂载的空目录
    with pytest.raises(ColdDirNotMountedError):
        archive(md, sd, cd, execute=True)
    assert raw.exists()


def test_dry_run_does_not_require_cold_sentinel(tmp_path: Path):
    """预览不触碰文件系统，不必强制要求已挂载——只有真正要写盘（execute=True）
    时才需要哨兵确认。"""
    md, sd, cd, raw = _setup(tmp_path)   # cd 未创建、也没有哨兵
    moved, skipped = archive(md, sd, cd)   # execute 默认 False，不应报错
    assert moved == 1 and skipped == 0
