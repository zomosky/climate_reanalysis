"""CLI：只测「参数→调用」翻译得对不对，不重新验证各单元自身的业务逻辑
（那些测试见 test_download.py / test_restore.py / test_migrate.py）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from climate_reanalysis import cli
from climate_reanalysis.settings import Settings
from climate_reanalysis.store import ensure_store, read_provenance, write_variable
from climate_reanalysis.variables import VAR_SPECS


def _settings_yaml(tmp_path: Path) -> Path:
    """写一份最小 config/settings.yaml——相对路径按 settings.load_settings 的
    锚定规则（config/ 的父目录）解析到 tmp_path 下。"""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg = cfg_dir / "settings.yaml"
    cfg.write_text(
        "raw_dir: raw\nmanifest_dir: manifests\nstore_dir: store\n", encoding="utf-8"
    )
    return cfg


def test_list_variables_prints_all_output_vars(capsys):
    assert cli.main(["list-variables"]) == 0
    out = capsys.readouterr().out
    for name in VAR_SPECS:
        assert name in out


def test_download_nothing_ready_returns_zero_without_network(tmp_path):
    cfg = _settings_yaml(tmp_path)
    # 起始月份远在未来，必然未就绪——不触碰网络（不需要 .cdsapirc）就该直接返回 0
    rc = cli.main(["--config", str(cfg), "download", "--start", "209901",
                  "--today", "2026-08-13"])
    assert rc == 0


def test_restore_on_empty_manifest_dir_is_a_noop(tmp_path):
    cfg = _settings_yaml(tmp_path)
    assert cli.main(["--config", str(cfg), "restore"]) == 0


def test_migrate_stamps_store_and_restore_then_leaves_it_alone(tmp_path):
    cfg = _settings_yaml(tmp_path)
    # 与 cfg 里的相对路径（锚定到 tmp_path）保持一致，直接构造 Settings 去操作月库
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "manifests",
                store_dir=tmp_path / "store")
    ensure_store(s.store_dir, "202601")
    data = np.zeros((31 * 24, 161, 281), dtype="float32")
    write_variable(s.store_dir, "202601", "ssrd", data, {})

    rc = cli.main(["--config", str(cfg), "migrate", "--months", "202601",
                  "--variables", "ssrd", "--tag", "legacy:x"])
    assert rc == 0
    prov = read_provenance(s.store_dir, "202601")
    assert prov["ssrd"]["source"] == "legacy:x"

    # manifest 目录是空的（没下载过），restore 因此无事可做——不会把刚打的 legacy 标记冲掉
    assert cli.main(["--config", str(cfg), "restore"]) == 0
    assert read_provenance(s.store_dir, "202601")["ssrd"]["source"] == "legacy:x"
