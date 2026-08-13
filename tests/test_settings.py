"""配置加载：相对路径锚定、代理与账号可为空、出厂默认值。"""

from __future__ import annotations

from pathlib import Path

import pytest

from climate_reanalysis.settings import Settings, load_settings


def test_defaults_match_spec():
    s = Settings()
    assert s.publish_lag_days == 5          # 实测 T-5 可得、T-3 不可得
    assert s.area == (55.0, 70.0, 15.0, 140.0)   # N W S E，与产出网格同框
    assert s.concurrency == 4
    assert s.dl_proxy is None               # 不得硬编码代理


def test_relative_paths_anchor_to_project_root(tmp_path: Path):
    cfg = tmp_path / "config" / "settings.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "raw_dir: data/raw\nmanifest_dir: data/manifests\nstore_dir: data/monthly\n",
        encoding="utf-8",
    )
    s = load_settings(cfg)
    # 锚定到 config/ 的父目录，而非进程 CWD——CLI 从任何目录调用都一致
    assert s.raw_dir == tmp_path / "data" / "raw"
    assert s.store_dir == tmp_path / "data" / "monthly"


def test_absolute_paths_kept(tmp_path: Path):
    cfg = tmp_path / "config" / "settings.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f"store_dir: {tmp_path / 'abs'}\n", encoding="utf-8")
    assert load_settings(cfg).store_dir == tmp_path / "abs"


def test_missing_file_falls_back_to_defaults():
    s = load_settings(None)
    assert s.publish_lag_days == 5
