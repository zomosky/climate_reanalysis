"""manifest：唯一的就绪信号，也是幂等比对的依据。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from climate_reanalysis import manifest as manifest_mod
from climate_reanalysis.manifest import (
    iter_manifests,
    manifest_path,
    manifest_sha256,
    read_manifest,
    write_manifest,
)


def _raw(tmp_path: Path, name: str, body: bytes = b"x" * 100) -> Path:
    p = tmp_path / name
    p.write_bytes(body)
    return p


def test_write_and_read_roundtrip(tmp_path: Path):
    md = tmp_path / "manifests"
    f = _raw(tmp_path, "ssrd.nc")
    p = write_manifest(md, "202601", "ssrd", [f], (55.0, 70.0, 15.0, 140.0))
    assert p == manifest_path(md, "202601", "ssrd")
    d = read_manifest(p)
    assert d["schema_version"] == 1
    assert d["source"]["name"] == "era5"
    assert d["source"]["dataset"] == "reanalysis-era5-single-levels"
    assert d["period"] == "202601"
    assert d["variable"] == "ssrd"
    assert d["area"] == [55.0, 70.0, 15.0, 140.0]
    assert d["completed_at"]
    assert d["files"][0]["size_bytes"] == 100
    assert len(d["files"][0]["sha256"]) == 64
    assert Path(d["files"][0]["path"]).is_absolute()


def test_two_cds_files_for_one_output_variable(tmp_path: Path):
    """si10 来自两个 CDS 变量——同属一份 manifest 的 files[]。"""
    md = tmp_path / "manifests"
    a, b = _raw(tmp_path, "u10.nc"), _raw(tmp_path, "v10.nc", b"y" * 50)
    d = read_manifest(write_manifest(md, "202601", "si10", [a, b],
                                     (55.0, 70.0, 15.0, 140.0)))
    assert len(d["files"]) == 2


def test_sha_changes_when_content_changes(tmp_path: Path):
    """溯源比对靠这个 sha——内容变了它必须变。"""
    md = tmp_path / "manifests"
    f = _raw(tmp_path, "ssrd.nc")
    p1 = write_manifest(md, "202601", "ssrd", [f], (55.0, 70.0, 15.0, 140.0))
    s1 = manifest_sha256(p1)
    f.write_bytes(b"z" * 200)
    p2 = write_manifest(md, "202601", "ssrd", [f], (55.0, 70.0, 15.0, 140.0))
    assert manifest_sha256(p2) != s1


def test_write_refuses_empty_files(tmp_path: Path):
    """只在成功时写 manifest——没有文件就不该有 manifest。"""
    with pytest.raises(ValueError):
        write_manifest(tmp_path / "m", "202601", "ssrd", [],
                       (55.0, 70.0, 15.0, 140.0))


def test_iter_manifests_sorted(tmp_path: Path):
    md = tmp_path / "manifests"
    f = _raw(tmp_path, "a.nc")
    for month, var in (("202602", "t2m"), ("202601", "ssrd"), ("202601", "t2m")):
        write_manifest(md, month, var, [f], (55.0, 70.0, 15.0, 140.0))
    assert [p.stem for p in iter_manifests(md)] == [
        "202601_ssrd.manifest", "202601_t2m.manifest", "202602_t2m.manifest",
    ]


def test_relative_path_is_stored_absolute(tmp_path: Path, monkeypatch):
    """manifest 是跨进程产物：restore 可能在别的工作目录读它。

    记相对路径等于记「相对于当时那个 CWD」，换个地方就解析不到。
    """
    monkeypatch.chdir(tmp_path)
    _raw(tmp_path, "ssrd.nc")
    d = read_manifest(write_manifest(tmp_path / "m", "202601", "ssrd",
                                     [Path("ssrd.nc")],
                                     (55.0, 70.0, 15.0, 140.0)))
    stored = Path(d["files"][0]["path"])
    assert stored.is_absolute()
    assert stored.read_bytes() == b"x" * 100      # 绝对化后仍指向同一个文件


def test_tmp_path_differs_per_process(tmp_path: Path, monkeypatch):
    """并发写不得互踩：tmp 名必须随进程变化。

    cron 上一轮没跑完下一轮就起来，是定时管线的常态。tmp 名若是确定性的，
    两个进程会打开同一个文件互相截断，交错出一份损坏的 JSON 再 replace 成
    正式 manifest——正好是这个模块存在的意义所要防的那种失败。
    """
    md = tmp_path / "manifests"
    f = _raw(tmp_path, "ssrd.nc")
    seen = []

    real_replace = Path.replace

    def spy(self, target):
        seen.append(self.name)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    for pid in (1111, 2222):
        monkeypatch.setattr(manifest_mod.os, "getpid", lambda pid=pid: pid)
        write_manifest(md, "202601", "ssrd", [f], (55.0, 70.0, 15.0, 140.0))

    assert seen[0] != seen[1], f"两个进程用了同一个 tmp 名: {seen[0]}"


def test_tmp_is_not_enumerated_as_ready_signal(tmp_path: Path):
    """半成品 tmp 绝不能被当成就绪信号——它一旦被 restore 读到就是半个月的数据。"""
    md = tmp_path / "manifests"
    md.mkdir()
    (md / "202601_ssrd.manifest.999999.tmp").write_bytes(b"{half-written")
    f = _raw(tmp_path, "ssrd.nc")
    p = write_manifest(md, "202601", "t2m", [f], (55.0, 70.0, 15.0, 140.0))
    assert iter_manifests(md) == [p]
