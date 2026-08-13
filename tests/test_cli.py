"""CLI：只测「参数→调用」翻译得对不对，不重新验证各单元自身的业务逻辑
（那些测试见 test_download.py / test_restore.py / test_migrate.py）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

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


def _forbid_network(*_args, **_kwargs):
    """打给 make_client/download_many 的桩：调用即失败。

    这条防线不依赖"被测代码会在碰到网络之前提前 return"——那是一个关于业务逻辑的
    假设，会随实现变化，之前就因为这个假设失效而真的建出了一个 CDS 客户端并提交了
    真实请求（占用了正在跑 20 年历史回填的同一账号的排队配额）。把 make_client 和
    download_many 在 ``climate_reanalysis.cli`` 这个引用点上钉成"调用即抛错"，
    使这类用例从结构上就不可能碰到真实网络：哪怕 cmd_download 的内部实现改了、
    --start 的必填校验被误删，最坏结果也只是断言失败，不会再发生真的出网。
    """
    raise AssertionError("这条用例不应该走到真实网络/CDS 客户端")


def test_download_nothing_ready_returns_zero_without_network(tmp_path, monkeypatch):
    cfg = _settings_yaml(tmp_path)
    monkeypatch.setattr("climate_reanalysis.cli.make_client", _forbid_network)
    monkeypatch.setattr("climate_reanalysis.cli.download_many", _forbid_network)
    # 起始月份远在未来，必然未就绪——不触碰网络（不需要 .cdsapirc）就该直接返回 0
    rc = cli.main(["--config", str(cfg), "download", "--start", "209901",
                  "--today", "2026-08-13"])
    assert rc == 0


def test_download_requires_explicit_start(tmp_path, monkeypatch):
    """--start 无默认值——migrate 只写月库溯源、不产生 manifest，download 的跳过
    判据只看 manifest 在不在，两者互不知情。如果 --start 有个"看起来无害"的默认值
    （比如 200501），裸跑一次 download 就会把已迁移的 20 年历史月份当成"从未下载"
    重新发起真实 CDS 请求——这个坑必须从源头（参数必填）堵住，不能靠文档提醒。"""
    cfg = _settings_yaml(tmp_path)
    monkeypatch.setattr("climate_reanalysis.cli.make_client", _forbid_network)
    monkeypatch.setattr("climate_reanalysis.cli.download_many", _forbid_network)
    with pytest.raises(SystemExit):
        cli.main(["--config", str(cfg), "download"])


def test_explicit_missing_config_errors_instead_of_running_with_defaults(tmp_path):
    """显式传入却不存在的 --config 必须报错——静默退回默认配置会表现成
    "处理 0 个、退出码 0"，运维会以为一切正常（真实场景：manifest_dir 打错，
    restore 对着一个空目录扫，什么都没做却"成功"退出）。"""
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(FileNotFoundError):
        cli.main(["--config", str(missing), "restore"])


def test_no_config_flag_falls_back_to_defaults_when_conventional_path_absent(
    tmp_path, monkeypatch
):
    """不传 --config 时走约定路径 config/settings.yaml；那个路径本身不存在
    （例如刚 clone 仓库还没 cp 配置样例）不该报错——退回出厂默认值，无害子
    命令（这里用 restore：默认 manifest_dir 下没有任何 manifest）照常返回 0。"""
    monkeypatch.chdir(tmp_path)   # cwd 下没有 config/settings.yaml
    assert cli.main(["restore"]) == 0


def test_cleanup_jobs_is_a_separate_explicit_entry_point(tmp_path, monkeypatch):
    """cleanup-jobs 是账号级清理（不限本批）——必须是一个要显式敲出来的独立
    子命令，且加 --yes 时要调用 cleanup_account_failed_jobs 而不是
    download_many 收尾那个只清本轮的 cleanup_failed_jobs。"""
    cfg = _settings_yaml(tmp_path)

    class FakeClient:
        pass

    captured = {}

    def fake_cleanup(client, statuses):
        captured["statuses"] = statuses
        return 3

    monkeypatch.setattr("climate_reanalysis.cli.make_client", lambda s: FakeClient())
    monkeypatch.setattr("climate_reanalysis.cli.cleanup_account_failed_jobs", fake_cleanup)

    rc = cli.main(["--config", str(cfg), "cleanup-jobs", "--yes"])

    assert rc == 0
    assert captured["statuses"] == ("failed", "rejected")


def test_cleanup_jobs_defaults_to_dry_run_and_deletes_nothing(tmp_path, monkeypatch, capsys):
    """账号级清理零防呆：敲错账号/配置即刻执行、不可撤销（同账号可能正跑着
    别人的 20 年历史回填）。不加 --yes 必须是纯预览——一次删除都不发生，哪怕
    账号上确实有一堆失败 job 可删。这里把 cleanup_account_failed_jobs 打成
    "调用即失败"，从结构上钉死默认路径不可能碰到真删。"""
    cfg = _settings_yaml(tmp_path)

    class FakeClient:
        pass

    def _forbid_delete(*_args, **_kwargs):
        raise AssertionError("dry-run 默认行为不该调用真正执行删除的函数")

    def fake_preview(client, statuses):
        return {"failed": ["job-1", "job-2"], "rejected": ["job-3"]}

    monkeypatch.setattr("climate_reanalysis.cli.make_client", lambda s: FakeClient())
    monkeypatch.setattr("climate_reanalysis.cli.cleanup_account_failed_jobs", _forbid_delete)
    monkeypatch.setattr("climate_reanalysis.cli.preview_account_failed_jobs", fake_preview)

    rc = cli.main(["--config", str(cfg), "cleanup-jobs"])   # 不加 --yes

    assert rc == 0
    out = capsys.readouterr().out
    assert "cleanup_jobs_preview" in out
    assert "cleanup_jobs_dry_run" in out


def test_cleanup_jobs_without_yes_never_calls_real_preview_deletion(tmp_path, monkeypatch):
    """即使不打桩 preview（走真实的 preview_account_failed_jobs 实现），只要
    没加 --yes，真正执行删除的 cleanup_account_failed_jobs 也绝不能被调用。"""
    cfg = _settings_yaml(tmp_path)

    class FakeClient:
        def get_jobs(self, **_kwargs):
            class _Page:
                request_ids = ["job-1", "job-2"]
            return _Page()

    def _forbid_delete(*_args, **_kwargs):
        raise AssertionError("dry-run 默认行为不该调用真正执行删除的函数")

    monkeypatch.setattr("climate_reanalysis.cli.make_client", lambda s: FakeClient())
    monkeypatch.setattr("climate_reanalysis.cli.cleanup_account_failed_jobs", _forbid_delete)

    assert cli.main(["--config", str(cfg), "cleanup-jobs"]) == 0


def test_download_and_restore_never_call_account_wide_cleanup(tmp_path, monkeypatch):
    """download/restore 收尾都不该碰账号级清理——那是 cleanup-jobs 独有的、
    需要显式调用的危险操作。"""
    cfg = _settings_yaml(tmp_path)
    monkeypatch.setattr("climate_reanalysis.cli.cleanup_account_failed_jobs", _forbid_network)

    assert cli.main(["--config", str(cfg), "restore"]) == 0
    # download：起始月份远在未来，未就绪、不联网，同样不该碰账号级清理
    assert cli.main(["--config", str(cfg), "download", "--start", "209901",
                     "--today", "2026-08-13"]) == 0


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
