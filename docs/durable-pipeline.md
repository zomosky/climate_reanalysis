# ERA5 持久化更新与版本发布

## 入口与边界

`sync` 是生产用的幂等单趟入口，覆盖补新、CDS 排队续接、文件下载、切片、校验和发布。每趟只查询一次未完成 CDS 任务的状态；任务仍在排队属于正常等待。配置路径必须显式传入。上线前需要同步项目依赖、准备 CDS 账号和数据挂载。

```bash
uv run climate_reanalysis --config config/settings.yaml sync --start 202608
uv run climate_reanalysis --config config/settings.yaml sync --start 202608 --refresh
uv run climate_reanalysis --config config/settings.yaml status
```

- `--start` 只限定补新范围。`--refresh` 还会检查起始月份之前的已有月库，防止更早的待换新月份被漏掉。
- 月末后 `publish_lag_days` 天才计划整月数据。默认 5 天；不追加本月逐日数据。
- `max_active_months` 默认 2；跨月份、跨轮次统计每账号尚未结束的 CDS 任务，各账号受 `concurrency`（默认 4）限制。
- 账号标识由凭据摘要计算，调整账号配置顺序不会错接任务。凭据、签名下载 URL 不写入状态。正在使用的账号不可移除；路径改变会拒绝续跑，避免把已完成下载误当缺失。
- 已有传统月库按变量溯源判断需要补哪些变量，保留原文件。首次托管不全量重下历史数据；缺少溯源的历史库应先审计，不应直接把 `--start` 指向历史起点。

## 目录与交接协议

| 位置 | 含义 |
|---|---|
| `<store_dir>/.pipeline/state.json` | 持久化任务阶段、CDS job id、尝试次数、退避时间和错误 |
| `<store_dir>/.pipeline/history/` | 已结束/被显式重新发起的任务历史 |
| `<store_dir>/.pipeline/_runs/` | 每轮调度摘要，只做审计，不是就绪信号 |
| `<raw_dir>/.pipeline/<task_id>/<YYYYMM>/*.nc` | 该任务的原始文件，`.part` 尚未完成 |
| `<manifest_dir>/.pipeline/<task_id>/*.manifest.json` | 原子发布的下载 manifest，沿用原来的 schema |
| `<store_dir>/.pipeline/work/<task_id>/candidate/` | 未发布的候选月库 |
| `<store_dir>/.versions/<YYYYMM>/<task_id>/<YYYY>/<YYYYMM>_era5.zarr` | 已验证、保留的不可变版本 |
| `<store_dir>/publication.json` | 原子发布索引，是读取侧选择托管版本的唯一依据 |

状态用于恢复执行；下载是否可切片仍由 manifest 决定，发布内容的溯源仍记录对应 manifest SHA。任务状态不替代数据事实。

下载完成条件：远端大小探测成功、本地大小严格相等、原子改名；记录本地 SHA 后才生成 manifest。续跑会校验已下载文件的 SHA；损坏不会被当作缓存命中。CDS 排队任务 ID 已保存时只恢复该任务，不重新提交。终态失败在退避后只清理本任务的失败 job，再提交新请求；不做账号级清理。

## 阶段和恢复

```text
pending_download → pending_restore → pending_validation → complete
                                                     ↘ waiting_final
```

错误保留当前阶段，另记 `last_error`、`attempts` 和 `next_retry_at`。默认首次失败等待 1 小时，指数退避到最多 24 小时；不要用进程退出码 0 判断月库已经齐全，要看 `status` 和发布索引。`sync` 有失败返回 1，正常排队/退避且没有错误返回 0；锁冲突拒绝启动，不重叠运行。

- 下载成功、切片失败：下一轮只续切片，不重新下载。
- 候选校验失败：不修改发布索引，原版本继续可读；修复原因后可重建候选。
- 进程在发布索引切换后退出：下一轮从索引确认已发布，补记完成，不再发起新版本。
- 提交前写入意图、提交后立即保存 job id。若进程恰好在远端受理后、回执保存前退出，状态为 `submitting`，不会盲目重新提交。CDS 不提供这里可用的客户端幂等键，因此这个极小窗口需核对账号任务。

```bash
# 核对 CDS 的账号、月份、变量后，关联已存在的远端任务。
# 命令会通过远端 collection_id/request 再核对，不等待任务运行完成。
uv run climate_reanalysis --config config/settings.yaml attach-job \
  --month 202608 --variable 2m_temperature --request-id <job-id>

# 只有确认 CDS 没有相应任务后，才允许重新提交。
uv run climate_reanalysis --config config/settings.yaml retry-submission \
  --month 202608 --variable 2m_temperature --confirm-no-remote-job

# 候选切片/验证失败，使用已有原始文件重新构建。
uv run climate_reanalysis --config config/settings.yaml retry-task --month 202608
# 原始数据本身需重新获取时，开新尝试，保留旧尝试及其数据。
uv run climate_reanalysis --config config/settings.yaml retry-task --month 202608 --redownload
```

`retry-task` 不允许丢弃仍在排队或提交结果不明确的 job。普通网络故障由定时退避续跑，不需要人工重置。

## ERA5T 换新

`--refresh` 根据当前可读月库的变量溯源计划任务：包含 `0005` 且月末严格超过 `refresh_after_days`（默认 100）才尝试。新下载使用新的工作目录，**不删除旧 raw、manifest 或月库**。

候选通过完整性校验后，若待换新变量仍不是纯 `0001`，状态为 `waiting_final`，旧发布版本保持不变；至少等待 `final_retry_days`（默认 7）后才计划新尝试。没有 expver 的变量不会被自动当作 ERA5T 重刷。

旧 `refresh --yes` 是破坏缓存的历史入口。产生托管任务后，旧 `download/restore/refresh --yes/migrate/bake-legacy/archive --yes` 命令拒绝写入该存储根，避免绕过候选发布流程。未托管目录的原有行为保持兼容。

## 验证与发布

候选会复制现有月库（不使用硬链接），只重建本次变化的变量。首次新月必须集齐九个变量。发布前检查：

1. consolidated 元数据、原生 UTC 整点轴、完整月小时数、经纬网格。
2. 九变量形状、单位和溯源；本轮变量溯源须匹配下载 manifest SHA。
3. 分空间块读完所有变量和时刻，拒绝 NaN/Inf/缺失块。
4. 发布索引注明变量 expver、质量状态、任务 ID、发布时间及新 revision。

完整候选移动到不可变版本目录，数据落盘后通过同目录原子 rename 切换 `publication.json`。读端先读取一次索引，再固定打开版本目录；旧版本保留，所以切换过程中已开始的惰性查询仍可完成。旧的传统月库也保留，新索引覆盖相应月份，不覆盖其他历史月份。

这意味着第三方读取端必须遵守索引协议，不能仅扫描传统 `<YYYY>/<YYYYMM>_era5.zarr` 路径。`climate_zrread.catalog.obs.ObsCatalog` 已提供兼容支持；没有索引时维持原来的月/日库发现方式，索引损坏或版本丢失时明确报错，不静默退回过期数据。Watcher 的结果和数据集缓存键包含索引指纹，无需重启服务即可在下一次查询使用新版本。

旧版本/工作目录不自动回收，初期会增加磁盘占用。后续清理必须考虑仍在运行的读请求和原始数据保留策略。任务状态目录和发布索引需要与数据一起备份，不能只拷月库。

## 241 调度接入

`deploy/era5_sync.sh` 是待安装的宿主机包装脚本，不会自行部署或修改 crontab。容器内由项目自带 `flock` 对同一个存储根加锁，托管 `sync` 和恢复命令（手动或 cron）都遵守。旧命令维持独立下载/切片行为；首次切换到 `sync` 前必须停止旧调度并等在途旧任务结束，避免接管瞬间与旧月库写入竞争。与 NWP 包装脚本不同，ERA5 的退出码 1 必须保留为失败。

可每 10 分钟调用一次 `sync`：日期规则决定是否产生新月任务，CDS 队列由短轮查询推进；`--refresh` 可每轮开启，7 天的候选重试间隔会阻止重复换新。也可只在每周固定一天设置 `ERA5_REFRESH=1`。不要并行另挂 legacy restore。

包装脚本需要显式设置 `ERA5_START`，支持 `ERA5_CONFIG`、`ERA5_PROJECT_DIR`、`CONTAINER` 和 `ERA5_REFRESH`。原有 CPU/内存限制仍由 `zhangmy-dev` 管理。首次上线应先核对存储挂载、CDS 凭据和历史溯源，再执行小月份验证。此实现未安装任何线上调度、未发出真实 CDS 请求。

空跑（未产生任何任务、未发布索引）不会禁止旧命令。传统下载不要求月库磁盘可写；其文件大小判据和非正并发值的既有处理保持兼容，严格校验和正并发约束仅用于 `sync`。
