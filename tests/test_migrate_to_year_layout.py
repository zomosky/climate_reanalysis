"""一次性迁移脚本：扁平月库 -> 按年分层。全程只碰 tmp_path，不触碰真实数据。"""

from __future__ import annotations

from pathlib import Path

from climate_reanalysis.store import store_path
from scripts.migrate_to_year_layout import MigrationPlan, main, migrate, plan_migration


def _make_flat_store(store_dir: Path, month: str) -> Path:
    """伪造一个「旧扁平布局」的月库——只需要是个目录，搬运不关心内容。"""
    p = store_dir / f"{month}_era5.zarr"
    p.mkdir(parents=True)
    (p / "zarr.json").write_text("{}")
    return p


def test_plan_migration_lists_flat_stores_only(tmp_path: Path):
    _make_flat_store(tmp_path, "202501")
    _make_flat_store(tmp_path, "202512")
    plans = plan_migration(tmp_path)
    assert [p.month for p in plans] == ["202501", "202512"]
    assert plans[0].dst == store_path(tmp_path, "202501")


def test_plan_migration_ignores_already_nested_stores(tmp_path: Path):
    """已经按年分层的库不是候选——只扫 store_dir 的直接子项，年份目录本身
    不匹配扁平命名正则。"""
    nested = tmp_path / "2026" / "202601_era5.zarr"
    nested.mkdir(parents=True)
    assert plan_migration(tmp_path) == []


def test_dry_run_moves_nothing(tmp_path: Path):
    src = _make_flat_store(tmp_path, "202501")
    todo, conflicts = migrate(tmp_path, execute=False)
    assert [p.month for p in todo] == ["202501"]
    assert conflicts == []
    assert src.exists()                              # 一个都没动
    assert not store_path(tmp_path, "202501").exists()


def test_execute_moves_via_rename(tmp_path: Path):
    src = _make_flat_store(tmp_path, "202501")
    todo, conflicts = migrate(tmp_path, execute=True)
    assert [p.month for p in todo] == ["202501"]
    assert conflicts == []
    assert not src.exists()
    dst = store_path(tmp_path, "202501")
    assert dst.is_dir()
    assert (dst / "zarr.json").exists()               # 内容随目录一起搬过去


def test_migration_is_idempotent_second_run_is_a_no_op(tmp_path: Path):
    _make_flat_store(tmp_path, "202501")
    first_todo, first_conflicts = migrate(tmp_path, execute=True)
    assert len(first_todo) == 1 and first_conflicts == []

    second_todo, second_conflicts = migrate(tmp_path, execute=True)
    assert second_todo == []
    assert second_conflicts == []
    assert store_path(tmp_path, "202501").is_dir()     # 数据仍在正确位置


def test_existing_destination_is_skipped_not_overwritten(tmp_path: Path, capsys):
    """目标已存在（理论上不该发生，但保守起见要防）：报错点名、跳过、不覆盖，
    且不阻塞其它月库继续搬。"""
    src = _make_flat_store(tmp_path, "202501")
    dst = store_path(tmp_path, "202501")
    dst.parent.mkdir(parents=True)
    dst.mkdir()
    (dst / "marker.txt").write_text("real-destination-content")

    _make_flat_store(tmp_path, "202502")   # 另一个正常月份，应正常搬走

    todo, conflicts = migrate(tmp_path, execute=True)
    assert [p.month for p in todo] == ["202502"]
    assert [p.month for p in conflicts] == ["202501"]
    assert src.exists()                                 # 冲突的源没被动过
    assert (dst / "marker.txt").read_text() == "real-destination-content"  # 目标未被覆盖
    assert store_path(tmp_path, "202502").is_dir()       # 其它月份不受影响

    out = capsys.readouterr().out
    assert "migrate_year_layout_conflict" in out and "202501" in out


def test_main_dry_run_exits_zero_and_touches_nothing(tmp_path: Path, capsys):
    src = _make_flat_store(tmp_path, "202501")
    rc = main(["--store-dir", str(tmp_path)])
    assert rc == 0
    assert src.exists()
    assert not store_path(tmp_path, "202501").exists()
    out = capsys.readouterr().out
    assert "migrate_year_layout_dry_run" in out


def test_main_yes_actually_moves_and_exits_zero_without_conflicts(tmp_path: Path):
    _make_flat_store(tmp_path, "202501")
    rc = main(["--store-dir", str(tmp_path), "--yes"])
    assert rc == 0
    assert store_path(tmp_path, "202501").is_dir()


def test_main_returns_nonzero_when_a_conflict_is_skipped(tmp_path: Path):
    _make_flat_store(tmp_path, "202501")
    dst = store_path(tmp_path, "202501")
    dst.parent.mkdir(parents=True)
    dst.mkdir()

    rc = main(["--store-dir", str(tmp_path), "--yes"])
    assert rc == 1


def test_migration_plan_is_a_frozen_dataclass_for_safe_reuse():
    """回归防呆：MigrationPlan 不该意外变成可变对象——它被同时用于展示计划、
    日志与执行，误改字段会让日志与实际行为脱节。"""
    plan = MigrationPlan(month="202501", src=Path("a"), dst=Path("b"))
    assert plan.month == "202501"
