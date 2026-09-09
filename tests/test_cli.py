"""CLI：只测「参数→调用」翻译得对不对，不重新验证各单元自身的业务逻辑
（那些测试见 test_download.py / test_restore.py / test_migrate.py）。"""

from __future__ import annotations

import re

from pathlib import Path

import numpy as np
import pytest

from climate_reanalysis import cli
from climate_reanalysis.archive import COLD_SENTINEL_NAME, cold_path
from climate_reanalysis.manifest import manifest_path, manifest_sha256, write_manifest
from climate_reanalysis.settings import Settings
from climate_reanalysis.store import ensure_store, read_provenance, set_provenance, write_variable
from climate_reanalysis.variables import VAR_SPECS

_AREA = (55.0, 70.0, 15.0, 140.0)


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


def _settings_yaml_with_cold(tmp_path: Path) -> Path:
    """同 ``_settings_yaml``，多配一个 ``cold_dir``——archive 命令出厂拒绝
    未配置冷盘的情况，需要单独一份配置样例覆盖"已配置"的路径。"""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg = cfg_dir / "settings.yaml"
    cfg.write_text(
        "raw_dir: raw\nmanifest_dir: manifests\nstore_dir: store\ncold_dir: cold\n",
        encoding="utf-8",
    )
    return cfg


def _archivable_month(s: Settings) -> Path:
    """造一份「下游已固化」的 (月×变量)：raw 文件存在，月库溯源的
    ``manifest_sha256`` 与该 manifest 当前 sha 一致——``archivable()`` 判据
    通过。原始内容用任意字节即可，archive 只做文件级拷贝+sha 校验，不解析
    netCDF。"""
    raw = s.raw_dir / "202601" / "2m_temperature.nc"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"payload" * 100)
    mf = write_manifest(s.manifest_dir, "202601", "t2m", [raw], _AREA)
    ensure_store(s.store_dir, "202601")
    set_provenance(s.store_dir, "202601", "t2m",
                   {"manifest_sha256": manifest_sha256(mf)})
    return raw


def test_list_variables_prints_all_output_vars(capsys):
    assert cli.main(["list-variables"]) == 0
    out = capsys.readouterr().out
    for name in VAR_SPECS:
        assert name in out


def _forbid_network(*_args, **_kwargs):
    """打给 make_clients/download_many_accounts 的桩：调用即失败。

    这条防线不依赖"被测代码会在碰到网络之前提前 return"——那是一个关于业务逻辑的
    假设，会随实现变化，之前就因为这个假设失效而真的建出了一个 CDS 客户端并提交了
    真实请求（占用了正在跑 20 年历史回填的同一账号的排队配额）。把 make_clients 和
    download_many_accounts 在 ``climate_reanalysis.cli`` 这个引用点上钉成"调用即抛错"，
    使这类用例从结构上就不可能碰到真实网络：哪怕 cmd_download 的内部实现改了、
    --start 的必填校验被误删，最坏结果也只是断言失败，不会再发生真的出网。
    """
    raise AssertionError("这条用例不应该走到真实网络/CDS 客户端")


def test_download_nothing_ready_returns_zero_without_network(tmp_path, monkeypatch):
    cfg = _settings_yaml(tmp_path)
    monkeypatch.setattr("climate_reanalysis.cli.make_clients", _forbid_network)
    monkeypatch.setattr("climate_reanalysis.cli.download_many_accounts", _forbid_network)
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
    monkeypatch.setattr("climate_reanalysis.cli.make_clients", _forbid_network)
    monkeypatch.setattr("climate_reanalysis.cli.download_many_accounts", _forbid_network)
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

    monkeypatch.setattr("climate_reanalysis.cli.make_clients", lambda s: [FakeClient()])
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

    monkeypatch.setattr("climate_reanalysis.cli.make_clients", lambda s: [FakeClient()])
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

    monkeypatch.setattr("climate_reanalysis.cli.make_clients", lambda s: [FakeClient()])
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


def test_bake_legacy_defaults_out_to_store_dir(tmp_path, monkeypatch):
    """--out 留空时取 settings.store_dir——把数据处理收拢进本项目的意义就是
    档案位置由项目配置统一管，不必每次手敲路径。"""
    cfg = _settings_yaml(tmp_path)
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "manifests",
                store_dir=tmp_path / "store")
    tiles_dir = tmp_path / "tiles"
    tiles_dir.mkdir()
    (tiles_dir / "110000.zarr").mkdir()  # 假 tile，只需要能被 glob 到

    captured = {}

    def fake_bake(tiles, out_root, months, ledger, variables=None, hours=None):
        captured["out_root"] = Path(out_root)
        captured["months"] = months
        captured["tiles"] = [Path(t) for t in tiles]
        captured["variables"] = variables

    monkeypatch.setattr("climate_reanalysis.cli.run_legacy_bake", fake_bake)

    rc = cli.main(["--config", str(cfg), "bake-legacy", "--tiles", str(tiles_dir),
                  "--months", "202601"])

    assert rc == 0
    assert captured["out_root"] == s.store_dir
    assert captured["months"] == ["202601"]
    assert captured["tiles"] == [tiles_dir / "110000.zarr"]
    assert captured["variables"] is None


def test_bake_legacy_explicit_out_overrides_store_dir(tmp_path, monkeypatch):
    cfg = _settings_yaml(tmp_path)
    tiles_dir = tmp_path / "tiles"
    tiles_dir.mkdir()
    (tiles_dir / "110000.zarr").mkdir()
    out_dir = tmp_path / "elsewhere"

    captured = {}
    monkeypatch.setattr(
        "climate_reanalysis.cli.run_legacy_bake",
        lambda tiles, out_root, months, ledger, variables=None, hours=None:
            captured.update(out_root=Path(out_root)),
    )

    rc = cli.main(["--config", str(cfg), "bake-legacy", "--tiles", str(tiles_dir),
                  "--months", "202601", "--out", str(out_dir), "--variables", "t2m"])

    assert rc == 0
    assert captured["out_root"] == out_dir


def test_bake_legacy_rejects_empty_tiles_dir_without_calling_bake(tmp_path, monkeypatch):
    """tile 目录下没有任何 .zarr——响亮失败，绝不能悄悄跑一次"什么都没烘"就
    返回 0（那会让人误以为历史导入已经完成）。"""
    cfg = _settings_yaml(tmp_path)
    empty_dir = tmp_path / "empty_tiles"
    empty_dir.mkdir()

    monkeypatch.setattr("climate_reanalysis.cli.run_legacy_bake", _forbid_network)

    rc = cli.main(["--config", str(cfg), "bake-legacy", "--tiles", str(empty_dir),
                  "--months", "202601"])
    assert rc != 0


def _stale_era5t_month(tmp_path: Path, s: Settings) -> None:
    """造一个「过期未换新」的月份：月库溯源记着仍是初步值，且已有对应 manifest
    （模拟当初 download 真的跑过一次）。"""
    ensure_store(s.store_dir, "202601")
    set_provenance(s.store_dir, "202601", "t2m",
                   {"manifest_sha256": "sha", "expver": ["0005"]})
    raw = tmp_path / "x.nc"
    raw.write_bytes(b"x")
    write_manifest(s.manifest_dir, "202601", "t2m", [raw], _AREA)


def test_refresh_on_empty_store_is_a_noop(tmp_path):
    cfg = _settings_yaml(tmp_path)
    assert cli.main(["--config", str(cfg), "refresh"]) == 0


def test_refresh_defaults_to_dry_run_and_deletes_nothing(tmp_path, capsys):
    """删 manifest 会触发真实重下，代价很实在——不加 --yes 必须是纯预览，一个
    manifest 都不能少，哪怕确实有过期月份可刷。"""
    cfg = _settings_yaml(tmp_path)
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "manifests",
                store_dir=tmp_path / "store")
    _stale_era5t_month(tmp_path, s)

    rc = cli.main(["--config", str(cfg), "refresh", "--today", "2026-06-01"])

    assert rc == 0
    assert manifest_path(s.manifest_dir, "202601", "t2m").exists()
    out = capsys.readouterr().out
    assert "refresh_item" in out
    assert "refresh_dry_run" in out


def test_refresh_with_yes_deletes_only_the_manifest(tmp_path):
    """--yes 才真删，且只删 manifest——月库溯源（含 expver）一字不动，重建
    交给下一轮 download + restore 的 sha 比对自然触发。"""
    cfg = _settings_yaml(tmp_path)
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "manifests",
                store_dir=tmp_path / "store")
    _stale_era5t_month(tmp_path, s)

    rc = cli.main(["--config", str(cfg), "refresh", "--today", "2026-06-01", "--yes"])

    assert rc == 0
    assert not manifest_path(s.manifest_dir, "202601", "t2m").exists()
    assert read_provenance(s.store_dir, "202601")["t2m"]["expver"] == ["0005"]


def test_refresh_after_days_overrides_config_default(tmp_path):
    """--after-days 显式传参覆盖配置里的 refresh_after_days（出厂 100）——月末
    到 today 只过了 20 天，默认窗口下不该刷新，但传一个更短的窗口应当刷新。"""
    cfg = _settings_yaml(tmp_path)
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "manifests",
                store_dir=tmp_path / "store")
    _stale_era5t_month(tmp_path, s)

    rc = cli.main(["--config", str(cfg), "refresh", "--today", "2026-02-20"])
    assert manifest_path(s.manifest_dir, "202601", "t2m").exists()  # 默认窗口内，不刷新

    rc = cli.main(["--config", str(cfg), "refresh", "--today", "2026-02-20",
                  "--after-days", "10", "--yes"])
    assert rc == 0
    assert not manifest_path(s.manifest_dir, "202601", "t2m").exists()


def test_archive_requires_cold_dir_configured(tmp_path, capsys):
    """cold_dir 出厂 None——归档命令必须拒绝执行，不给一个看似无害的默认
    路径悄悄搬走原始文件（本机/多数环境根本没有那块盘）。裸异常回溯不该
    冒出 main()：必须是日志 + 非零退出码，而不是让调用方catch不到的原始
    异常炸穿整个 CLI 进程。"""
    cfg = _settings_yaml(tmp_path)   # 没有配 cold_dir
    rc = cli.main(["--config", str(cfg), "archive"])
    assert rc != 0
    out = capsys.readouterr().out
    assert "archive_failed" in out


def test_archive_with_yes_and_unmounted_cold_dir_logs_and_returns_nonzero(
    tmp_path, capsys
):
    """cold_dir 配了但没挂载（缺哨兵文件）——同样不能裸异常回溯冒出 main()，
    必须是日志 + 非零退出码，提示运维怎么处理。"""
    cfg = _settings_yaml_with_cold(tmp_path)
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "manifests",
                store_dir=tmp_path / "store", cold_dir=tmp_path / "cold")
    _archivable_month(s)   # cold_dir 从未创建、没放哨兵——模拟未挂载

    rc = cli.main(["--config", str(cfg), "archive", "--yes"])

    assert rc != 0
    out = capsys.readouterr().out
    assert "archive_failed" in out


def test_archive_on_empty_manifest_dir_is_a_noop(tmp_path):
    cfg = _settings_yaml_with_cold(tmp_path)
    assert cli.main(["--config", str(cfg), "archive"]) == 0


def test_archive_defaults_to_dry_run_and_deletes_nothing(tmp_path, capsys):
    """归档会清空热端原始文件，代价很实在——不加 --yes 必须是纯统计，一个
    文件都不能动，哪怕确实有已固化的 (月×变量) 可搬。"""
    cfg = _settings_yaml_with_cold(tmp_path)
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "manifests",
                store_dir=tmp_path / "store", cold_dir=tmp_path / "cold")
    raw = _archivable_month(s)

    rc = cli.main(["--config", str(cfg), "archive"])   # 不加 --yes

    assert rc == 0
    assert raw.exists()               # 一个都没动
    assert not s.cold_dir.exists()    # 连冷端目录都没建
    out = capsys.readouterr().out
    assert "archive_dry_run" in out


def test_archive_with_yes_moves_and_cleans_hot(tmp_path):
    """--yes 才真正搬运：冷端内容与热端一致、热端清空——`cmd_archive` 把
    `--yes` 正确映射到 `archive()` 的 `execute` 参数。"""
    cfg = _settings_yaml_with_cold(tmp_path)
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "manifests",
                store_dir=tmp_path / "store", cold_dir=tmp_path / "cold")
    raw = _archivable_month(s)
    body = raw.read_bytes()
    # execute=True 起要求冷盘已挂载确认（哨兵文件）——见 archive.py 的
    # ColdDirNotMountedError；这里模拟"已确认挂载"。
    s.cold_dir.mkdir(parents=True)
    (s.cold_dir / COLD_SENTINEL_NAME).touch()

    rc = cli.main(["--config", str(cfg), "archive", "--yes"])

    assert rc == 0
    assert not raw.exists()
    cold = cold_path(s.cold_dir, "202601", "2m_temperature.nc")
    assert cold.read_bytes() == body


def test_archive_returns_zero_when_everything_is_already_archived(tmp_path, capsys):
    """归档完成后的稳态必须是退出码 0。

    这是 cron 视角唯一要紧的语义：archive 挂上定时任务后，绝大多数轮次都是
    「该搬的都搬完了、无事可做」。若把这种稳态也当成非零退出，运维会天天收到
    报警，久而久之就没人看了——真正的搬运失败反而被淹没。所以「已归档」与
    「失败」必须走不同的退出码。
    """
    cfg = _settings_yaml_with_cold(tmp_path)
    s = Settings(raw_dir=tmp_path / "raw", manifest_dir=tmp_path / "manifests",
                store_dir=tmp_path / "store", cold_dir=tmp_path / "cold")
    _archivable_month(s)
    s.cold_dir.mkdir(parents=True, exist_ok=True)
    (s.cold_dir / COLD_SENTINEL_NAME).touch()

    assert cli.main(["--config", str(cfg), "archive", "--yes"]) == 0   # 第一轮真搬
    capsys.readouterr()

    rc = cli.main(["--config", str(cfg), "archive", "--yes"])          # 第二轮无事可做
    assert rc == 0
    # structlog 会在键与值之间插 ANSI 颜色码，先剥掉再断言
    out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert "already=1" in out and "failed=0" in out and "moved=0" in out


def test_status_is_json_and_does_not_create_state(tmp_path,capsys):
    import json
    cfg=_settings_yaml(tmp_path)
    assert cli.main(['--config',str(cfg),'status']) == 0
    payload=json.loads(capsys.readouterr().out)
    assert payload['tasks'] == {}
    assert not (tmp_path/'store').exists()


def test_sync_future_start_needs_no_credentials(tmp_path,monkeypatch):
    cfg=_settings_yaml(tmp_path)
    monkeypatch.setattr('climate_reanalysis.jobs.make_accounts',_forbid_network)
    assert cli.main(['--config',str(cfg),'sync','--start','209901']) == 0


def test_retry_submission_requires_explicit_absence_confirmation():
    with pytest.raises(SystemExit):
        cli.main(['retry-submission','--month','202608','--variable','2m_temperature'])


def test_legacy_download_does_not_require_store_mount(tmp_path,monkeypatch):
    cfg=_settings_yaml(tmp_path)
    # Legacy download only needs raw/manifest locations; its future no-op must
    # work even when the external store destination is unavailable for writes.
    (tmp_path/'store').write_text('not a mounted data directory')
    monkeypatch.setattr('climate_reanalysis.cli.make_clients',_forbid_network)
    assert cli.main(['--config',str(cfg),'download','--start','209901']) == 0
