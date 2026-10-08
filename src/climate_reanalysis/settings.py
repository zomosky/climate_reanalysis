"""配置：路径、代理、并发、发布延迟、区域。

**不得硬编码路径与代理**——测试期在本机（Clash 代理，实测 4.8 MB/s），后续迁到
服务器（当前无代理，裸连 CDS 仅 10.6 KB/s）。改位置应当只改配置。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 原始 netCDF 落盘（restore 成功后可删）
    raw_dir: Path = Path("data/raw")
    #: manifest 目录——唯一的就绪信号
    manifest_dir: Path = Path("data/manifests")
    #: 月度国网库（zrread 的 era5 源指向它）
    store_dir: Path = Path("data/monthly")
    #: 冷盘归档根目录；None = 未配置——archive 命令拒绝执行（本机/测试环境
    #: 通常没有这块盘，绝不能给一个看似无害的默认值让归档悄悄发生）。
    #: 配置后布局为 cold_dir/<YYYYMM>/<原始文件名>，与 raw_dir 同构
    cold_dir: Path | None = None
    #: bulk download 走的代理；None = 裸连。submit/poll 一律直连（trust_env=False）
    dl_proxy: str | None = None
    #: 每期并发下载的变量数。链路退化（日志频现 curl rc=18）时调低到 1-2
    concurrency: int = 4
    #: sync 同时推进的月份数；每个账号仍受 concurrency 限制。
    max_active_months: int = Field(default=2, ge=1)
    retry_hours: int = Field(default=1, ge=1)
    final_retry_days: int = Field(default=7, ge=1)
    #: Unknown submits: bounded, complete CDS lookups before reusing/retrying.
    submission_reconcile_interval_minutes: int = Field(default=10, ge=1)
    submission_reconcile_grace_minutes: int = Field(default=30, ge=1)
    submission_reconcile_absence_checks: int = Field(default=2, ge=2)
    submission_reconcile_max_pages: int = Field(default=20, ge=1)
    #: ERA5 发布延迟：月末再等这么多天才认为该月齐全（实测 T-5 可得、T-3 不可得）
    publish_lag_days: int = 5
    #: ERA5T 初步数据换新窗口：月末再等这么多天才认为该月的初步值（expver=0005）
    #: 已经"过期"、该重新拉一遍 final（final 实测滞后 2-3 个月）
    refresh_after_days: int = 100
    #: 请求区域 (N, W, S, E)，与产出网格同框
    area: tuple[float, float, float, float] = (55.0, 70.0, 15.0, 140.0)
    #: 指定 .cdsapirc；None = ~/.cdsapirc。按惯例是 home 下的凭据文件，故与上面三个
    #: 数据路径不同：不锚定项目根，只做 ~ 展开（多账号轮换时写 ~/.cdsapirc_xxx）
    cdsapirc: Path | None = None
    #: 账号池：多份 .cdsapirc，非空即启用多账号并行下载——（月×变量）任务轮转
    #: 分给各账号，每账号各守 concurrency 的排队上限，防单账号被 CDS 限流（思路
    #: 承自 solarcast：每个账号的排队大致半串行，两个账号 ≈ 2 倍吞吐）。路径语义
    #: 同 cdsapirc：只展开 ~、不锚定项目根。空列表 = 单账号（用上面的 cdsapirc）
    cdsapircs: list[Path] = []


def _anchor(base: Path, p: Path) -> Path:
    """相对路径锚定到项目根（config/ 的父目录），而非进程 CWD。"""
    return p if p.is_absolute() else (base / p).resolve()


def load_settings(path: Path | None = None) -> Settings:
    """``path=None`` 用出厂默认值（无需报错——CLI 用它表达"没有可用的配置文件，
    退回默认"这种约定俗成的场景）；显式给了一个路径就必须存在，不存在报错。

    这条不对称是有意的：此前不管 ``path`` 是 ``None`` 还是显式传入但打错的
    路径，缺文件都静默退回默认值——CLI 层面表现成"处理 0 个、退出码 0"，
    运维会以为一切正常。是否"显式"这件事由调用方（cli.py）判断并决定传
    ``None`` 还是具体路径，本函数只按"给没给路径"这一条分支。
    """
    if path is None:
        return Settings()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"--config 指定的配置文件不存在: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    s = Settings.model_validate(raw)
    base = path.parent.parent if path.parent.name == "config" else path.parent
    return s.model_copy(
        update={
            "raw_dir": _anchor(base, s.raw_dir),
            "manifest_dir": _anchor(base, s.manifest_dir),
            "store_dir": _anchor(base, s.store_dir),
            "cold_dir": _anchor(base, s.cold_dir) if s.cold_dir else None,
            "cdsapirc": s.cdsapirc.expanduser() if s.cdsapirc else None,
            "cdsapircs": [p.expanduser() for p in s.cdsapircs],
        }
    )
