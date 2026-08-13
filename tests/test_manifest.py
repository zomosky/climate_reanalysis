"""manifest：唯一的就绪信号，也是幂等比对的依据。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    assert d["period"] == "202601"
    assert d["variable"] == "ssrd"
    assert d["area"] == [55.0, 70.0, 15.0, 140.0]
    assert d["completed_at"]
    assert d["files"][0]["size_bytes"] == 100
    assert len(d["files"][0]["sha256"]) == 64


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
