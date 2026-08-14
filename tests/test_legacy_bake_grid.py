"""目标网格冻结、tile 落位、账本幂等。"""

from __future__ import annotations

import json

import numpy as np
import pytest

from climate_reanalysis import store
from climate_reanalysis.legacy_bake.grid import (
    CHUNK,
    SHARD,
    TARGET_LAT,
    TARGET_LON,
    Ledger,
    coverage_from_tiles,
    shard_slices,
    tile_coverage,
)


def test_target_grid_is_frozen_and_matches_ifs_hres():
    """与 ifs-hres 逐格相同——掩膜与 wmean 权重缓存靠这个前提复用。"""
    assert TARGET_LAT.size == 161
    assert TARGET_LON.size == 281
    assert TARGET_LAT[0] == 15.0 and TARGET_LAT[-1] == 55.0
    assert TARGET_LON[0] == 70.0 and TARGET_LON[-1] == 140.0
    assert np.allclose(np.diff(TARGET_LAT), 0.25)
    assert np.allclose(np.diff(TARGET_LON), 0.25)


def test_grid_constants_are_imported_from_store_not_redefined():
    """搬运时曾经发现 grid.py 自带一份 TARGET_LAT/TARGET_LON/SHARD/CHUNK 与
    store.py 的重复——两份真值一旦漂移，烘出来的网格会与 download/restore
    产出的月库对不上，且不会有任何报错。钉死 legacy_bake.grid 这几个名字
    确实是从 store 转发过来的同一份对象，防止将来又有人在这里重新定义。
    """
    assert TARGET_LAT is store.TARGET_LAT
    assert TARGET_LON is store.TARGET_LON
    assert SHARD is store.SHARD
    assert CHUNK is store.CHUNK


def test_shard_is_multiple_of_chunk():
    assert SHARD % CHUNK == 0


def test_shard_slices_tile_the_whole_grid_without_overlap():
    seen = np.zeros((TARGET_LAT.size, TARGET_LON.size), dtype=int)
    for la, lo in shard_slices():
        seen[la, lo] += 1
    assert seen.min() == 1 and seen.max() == 1


def test_tile_coverage_maps_interior_tile():
    tile_lat = np.arange(30.0, 32.01, 0.25)
    tile_lon = np.arange(110.0, 112.01, 0.25)
    cov = tile_coverage(tile_lat, tile_lon)
    assert cov is not None
    la, lo, rows, cols = cov
    assert TARGET_LAT[la][0] == 30.0
    assert TARGET_LON[lo][0] == 110.0
    assert rows.size == tile_lat.size and cols.size == tile_lon.size


def test_tile_coverage_clips_rows_below_15n():
    """三沙（<15N）落在框外，须被裁掉而不是报错——ifs-hres 本就在 15N 截断。"""
    tile_lat = np.arange(13.0, 16.01, 0.25)
    tile_lon = np.arange(110.0, 111.01, 0.25)
    la, lo, rows, cols = tile_coverage(tile_lat, tile_lon)
    assert TARGET_LAT[la][0] == 15.0
    assert rows.size == 5  # 15.00 15.25 15.50 15.75 16.00
    assert tile_lat[rows[0]] == 15.0


def test_tile_coverage_none_when_fully_outside():
    assert tile_coverage(np.array([3.0, 3.25]), np.array([110.0])) is None


def test_tile_coverage_rejects_off_lattice_tile():
    """格点必须落在 0.25° 点阵上，否则是数据问题，应当响亮失败。"""
    with pytest.raises(ValueError, match="0.25"):
        tile_coverage(np.array([30.1, 30.35]), np.array([110.0]))


def test_coverage_from_tiles_marks_only_covered_cells():
    cov = coverage_from_tiles(
        [
            (np.arange(30.0, 31.01, 0.25), np.arange(110.0, 111.01, 0.25)),
            (np.arange(40.0, 40.51, 0.25), np.arange(120.0, 120.51, 0.25)),
        ]
    )
    assert cov.shape == (TARGET_LAT.size, TARGET_LON.size)
    assert cov[np.searchsorted(TARGET_LAT, 30.5), np.searchsorted(TARGET_LON, 110.5)]
    assert not cov[np.searchsorted(TARGET_LAT, 50.0), np.searchsorted(TARGET_LON, 130.0)]
    assert cov.sum() == 5 * 5 + 3 * 3


def test_coverage_from_tiles_ignores_out_of_frame_tiles():
    cov = coverage_from_tiles([(np.array([3.0, 3.25]), np.array([110.0]))])
    assert cov.sum() == 0


def test_ledger_roundtrip_and_idempotence(tmp_path):
    p = tmp_path / "ledger.json"
    led = Ledger.load(p)
    assert not led.done("202601", "t2m", 3)
    led.mark("202601", "t2m", 3)
    led.save()

    again = Ledger.load(p)
    assert again.done("202601", "t2m", 3)
    assert not again.done("202601", "ssrd", 3)
    assert not again.done("202602", "t2m", 3)


def test_legacy_ledger_without_fingerprint_is_invalidated(tmp_path):
    """指纹机制之前写的账本（无 tiles 字段）来历不明，必须作废而非放行。

    曾经放行过：结果补下的 14 个新 tile 一个没进库，而新指纹还被写进账本，
    让它声称自己是最新的——静默陈旧，且事后看不出来。
    """
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"done": ["202601/t2m/0", "202601/ssrd/0"]}))
    led = Ledger.load(p, "newfingerprint00")
    assert not led.done("202601", "t2m", 0), "遗留账本应作废"
    assert led.tiles_fingerprint == "newfingerprint00"


def test_matching_fingerprint_keeps_ledger(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"tiles": "abc123", "done": ["202601/t2m/0"]}))
    led = Ledger.load(p, "abc123")
    assert led.done("202601", "t2m", 0), "指纹一致应续用账本"
