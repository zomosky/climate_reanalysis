# climate_reanalysis

ERA5 再分析的**持续积累**管线，与 `climate_pipeline` 里的 `download → climate_restorage`
同构：按月下载、每（月 × 产出变量）一份 manifest 作为就绪信号、restore 幂等地把它切进
月度国网库。

与 `solarcast` 的省库/TMY **是两件事**：那是发电量收益测算资产，有自己的用途与生命
周期；二者仅在“历史数据复用、免于重复下载”这一点上有交集（见下方「历史迁移」），
此后不再耦合。

产出被 `climate_zrread` 的 `era5` 观测源直接消费（网格与 `ifs-hres` 逐格相同，
产出契约见下文「月库目录结构」一节）。

## 三个单元

| 命令 | 作用 |
|---|---|
| `download` | 每（月 × 产出变量）一次 CDS 请求 → `raw/` + `manifests/`（唯一的就绪信号） |
| `restore` | 幂等一趟扫描 manifest → 写进 `<YYYY>/<YYYYMM>_era5.zarr`（按年分层，可 cron） |
| `migrate` | 给既有月库的历史变量打 legacy 溯源，使 restore 认为已就位，避免重下 |
| `bake-legacy` | 一次性历史导入：把 `solarcast` 省级 20 年 tile 直接烘成月度国网库（见下方「历史导入（一次性烘焙）」） |

## 安装

Python ≥3.11，用 `uv` 管理依赖（与仓库内其它子项目一致）：

```bash
uv sync                # 含 dev 组（pytest）
uv run pytest -q       # 全量测试，~10s，零真实网络依赖
```

## 配置

复制样例并按机器改：

```bash
cp config/settings.yaml.example config/settings.yaml
```

`config/settings.yaml`（不入版本库，机器相关）字段：

| 字段 | 含义 |
|---|---|
| `raw_dir` | 原始 netCDF 落盘（restore 成功后可删） |
| `manifest_dir` | manifest 目录——**唯一**的就绪信号 |
| `store_dir` | 月度国网库根目录，按年分层落盘（`climate_zrread` 的 `era5` 源指向它，见下方「月库目录结构」） |
| `dl_proxy` | **只用于 bulk download**，submit/poll 一律直连；留空 = 裸连 |
| `concurrency` | 每期并发下载的（月×变量）任务数，出厂 4 |
| `publish_lag_days` | ERA5 发布延迟（天），出厂 5——月末再等这么多天才认为该月齐全 |
| `refresh_after_days` | ERA5T 初步数据换新窗口（天），出厂 100——月末再等这么多天才认为初步值「过期」，见下方「ERA5T 刷新」 |
| `cold_dir` | 冷盘归档根目录；留空（出厂 `None`）= 未配置，`archive` 拒绝执行，见下方「冷存归档」 |
| `area` | 请求区域 `(N, W, S, E)`，需与产出网格 15–55N / 70–140E 一致 |
| `cdsapirc` | 指定 `.cdsapirc` 路径；留空 = `~/.cdsapirc` |
| `cdsapircs` | 账号池：多份 `.cdsapirc` 路径，非空即启用多账号并行下载；空/不配 = 单账号（用 `cdsapirc`） |

`raw_dir`/`manifest_dir`/`store_dir` 的相对路径锚定到 `config/` 的**父目录**（即项目根），
不是运行时的 CWD——在哪个目录下敲 `uv run` 都一样。

**不要把 `dl_proxy` 或任何绝对路径硬编码进代码**：本机测试期有 Clash 代理（实测
4.8 MB/s），部署服务器当前无代理（裸连 CDS 仅 10.6 KB/s）——只应通过
`config/settings.yaml` 按机器区分。

CDS 凭据：样例见 `config/cdsapirc.example`（CDS 官方标准格式，`url`/`key` 两行，
key 从 CDS 个人页获取）。复制到 `~/.cdsapirc`，或复制为 `config/cdsapirc`
（不入版本库）并在 `settings.yaml` 里 `cdsapirc: config/cdsapirc` 指过去。

**多账号并行**（防单账号限流，思路承自 solarcast）：有多个账号时各存一份 rc
文件，在 `cdsapircs` 里列出即启用账号池——（月×变量）任务轮转分给各账号并行
下载，**每账号各守 `concurrency` 的排队上限**、各自清理自己本轮的失败 job；
`cleanup-jobs` 也会逐账号预览/清理。每个 CDS 账号的排队大致半串行，两个账号
≈ 2 倍吞吐。只有一个账号就用 `cdsapirc` 单数形式。

## 月库目录结构（按年分层）

`store_dir` 下的月库按年分层：

```
<store_dir>/<YYYY>/<YYYYMM>_era5.zarr
```

例如 202601 落在 `<store_dir>/2026/202601_era5.zarr`，不再是早前的扁平
`<store_dir>/202601_era5.zarr`。这是持续积累管线的特性倒逼的改动——20 年历史
就是 240 个月库，此后每年还要新增 12 个，扁平堆在一个目录下之后按年 rsync/
校验/归档都不方便；`store_path()` 是唯一拼这个路径的地方（`ensure_store` 建库
时年份子目录随 `mkdir(parents=True)` 一起建出来，调用方不需要关心），
`bake-legacy`/`refresh` 等所有落盘/扫描路径都改成调它，不在别处各自拼一份。

**这是本项目的一次性结构调整，不是兼容层**——`climate_reanalysis` 是这份档案
唯一的生产者，不需要像下游 `climate_zrread` 的观测源读取端那样同时认得新旧两
种布局。已有的历史月库（扁平）需要手工迁移一次：

```bash
uv run python scripts/migrate_to_year_layout.py --store-dir /path/to/store_dir          # 默认 dry-run：只打印将搬运的清单，不动任何文件
uv run python scripts/migrate_to_year_layout.py --store-dir /path/to/store_dir --yes     # 确认清单无误后，加 --yes 才真正搬运
```

同一块盘内用 `os.rename` 挪目录，秒级完成，不是逐文件拷贝。幂等——已经在年份
子目录下的库不会被当作候选，重跑是空操作；目标位置意外已存在时报错点名并
跳过（不覆盖），不阻塞其余月库继续搬。

## download / restore 怎么调

（`migrate` 见下方「历史迁移」一节；账号级 job 清理见「CDS 失败 job 清理」一节）

```bash
# 列出九个产出变量与其 CDS 来源
uv run climate_reanalysis list-variables

# 下载：从 --start 起，按 publish_lag_days 判定的「已齐全」月份全部下载
# （--months 可显式指定月份列表，跳过 ready_months 的自动枚举）
uv run climate_reanalysis download --start 202501
uv run climate_reanalysis download --start 202501 --months 202501 --variables ssrdc

# restore：幂等一趟扫描，只重建溯源不一致/缺失的（月×变量），其余跳过
uv run climate_reanalysis restore
```

`--config` 是顶层参数，写在子命令**之前**：`climate_reanalysis --config path/to/settings.yaml download ...`。
不传时用约定的 `config/settings.yaml`（相对 CWD）——那个文件存在才用，不存在就退回出厂
默认值，不报错（方便刚 clone 仓库、还没 `cp` 配置样例时先跑 `list-variables` 之类不依赖
配置的子命令）。**显式传入的 `--config` 路径必须存在，不存在会报错**——不会像早期实现那样
静默退回默认配置、表现成"处理 0 个、退出码 0"让人误以为一切正常。

`--start` **必填、没有默认值**（哪怕搭配 `--months` 显式指定月份也要传，argparse 不看
业务逻辑）——这是有意的：见下方「历史迁移」一节末尾的说明，一个"看起来无害"的默认值
会在配合 `migrate` 使用时把整段历史重新引向真实网络请求。

`download` 未就绪时不联网、不报错，直接跳过并返回退出码 0（同月其它变量互不影响）；
`restore` 全部成功退出码 0，有已就绪的 manifest 处理失败则非 0（同 `climate_restorage`
的 `scan-once`）。

## CDS 失败 job 清理

失败/被拒的 CDS job 会一直占着排队配额，不清理会越积越多。`download` 每轮收尾会
**自动**清理，但**只清理这一轮自己 submit 得到的 job**，不碰账号上的其它任务——
同一个 CDS 账号上完全可能正跑着别的批次（例如另一段历史回填），账号级不分青红皂白
地全清会把它们一并删掉。

如果需要清空账号上**全部**失败/被拒 job（不限本批，例如账号里堆积了旧脚本、别的
项目留下的失败 job），用独立的子命令，需要显式敲出来：

```bash
uv run climate_reanalysis cleanup-jobs                    # 默认 dry-run：只预览，不删
uv run climate_reanalysis cleanup-jobs --statuses failed  # 预览时只看 failed，不看 rejected
uv run climate_reanalysis cleanup-jobs --yes              # 确认预览无误后，加 --yes 才真删
```

**这条命令不会被 `download`/`restore` 自动调用**——它是危险操作，误删同账号上别人
正跑着的任务没有撤销的余地，只应该在确认清楚账号上都是自己的任务时手动执行。**不加
`--yes` 是默认行为、且不做任何实际删除**：只按状态分类打印将要删除的数量与若干示例
id，供人工核对账号/统计对不对；确认无误后再加 `--yes` 重跑一次才会真正执行删除。

## ERA5T 刷新

ERA5 有两种成色：**ERA5T 初步数据**（`expver=0005`，滞后约 5 天可得，数值可能被
后续修订）与 **final 正式数据**（`expver=0001`，滞后 2–3 个月）。`restore` 会把
原始文件的 `expver` 坐标记进月库溯源（`zrread_provenance[变量].expver`），但**落盘
后没有任何自动换新通路**——初步值写进去之后，除非手动介入，永远不会被替换成 final。

`refresh` 补的就是这个通路。**它不去教 `download` 认得月库结构**——两个单元只通过
manifest 通信，这是刻意保持的契约——而是把过期月份对应的 manifest **连同原始文件
一起删掉**，让既有的 `download → restore` 流程自然把它们当成「没下过」重新走一遍：
新下载产生新 manifest，sha 与月库溯源里记的旧 sha 天然对不上，`restore` 就会按
幂等规则重建那个变量。`refresh` 本身**不联网、不重新写任何数据**，`execute=True`
时只删这两类「重下即可再生」的文件，绝不碰月库数据本身。

**必须连原始文件一起删，只删 manifest 是不够的**：`download_one` 判断要不要真的
发请求分两层——manifest 存在就整体早退，manifest 不存在则逐 CDS 变量看热端 raw
文件在不在（`target.exists()`）决定要不要调用 CDS。早期实现只删了 manifest、
留着 raw，第二层缓存判据会原封不动地命中同一份过期文件：`download_one` 在**零
网络请求**的情况下把旧文件原样重新写进新 manifest（`completed_at` 变了、sha 因此
也变，看起来像"处理过"），`restore` 因此重建出跟之前一模一样的初步数据——
`refresh` 变成一次静默空转，日志照常打 `refresh_done`，但 `expver` 永远还是
`0005`，永远刷不动。现在 `--yes` 会把 manifest 里记录的原始文件（热端 raw，
若配置了 `cold_dir` 则连冷端副本也）一并删掉，删除顺序固定为**先 raw、后
manifest**（manifest 是「已完整下载」的信号，必须最后消失——中途崩溃时留下
"manifest 在但 raw 已删一部分"是 `restore` 能安全跳过的无害状态，反过来先删
manifest 会留下无主的 raw 文件）。

判据：该 (月, 变量) 的溯源 `expver` 里含 `"0005"`（哪怕只是跨边界月份的一部分），
且该月月末距今已超过 `refresh_after_days` 天（出厂 100，留出比 final 实测 2–3 个月
发布滞后更宽松的余量）。**缺 `expver` 键的老条目一律视为不需要刷新**——`migrate`
打的 legacy 标记、更早版本写的溯源都没有这个字段，宁可漏刷，也不要把没记录这个
字段的历史数据（可能是几十 GB、几十小时排队量级）全部当成待刷新对象重下。

```bash
uv run climate_reanalysis refresh                              # 默认 dry-run：只打印清单，不删
uv run climate_reanalysis refresh --after-days 60               # 覆盖配置里的 refresh_after_days
uv run climate_reanalysis refresh --today 2026-06-01             # 覆盖“今天”，便于核对判据
uv run climate_reanalysis refresh --yes                          # 确认清单无误后，加 --yes 才真删（manifest + 原始文件）
```

**不加 `--yes` 是默认行为、不删任何文件**——删除会触发真实重下，代价是排队时间和
带宽，必须先看清楚清单再决定。**`refresh --yes` 执行完之后，必须再手动跑一次
`download` + `restore`**（`refresh` 自己不联网、不做这两步）：manifest 和 raw
都没了之后，`download` 才会把这些 (月, 变量) 当成「没下过」重新发起真实 CDS
请求，`restore` 再把新结果（连带新的 `expver`）切回月库，完成真正的换新。这条
通路与「raw 清理后月库损坏」一节里的手工恢复步骤是同一套底层机制（删 manifest +
raw → 触发 sha 不符 → 自然重建），`refresh` 只是把它包装成一个显式、可预览、按
ERA5T 判据自动找出目标的命令。

## 冷存归档

原始 netCDF（`raw_dir`）很占地方：单变量单月约 55 MB，九个变量、二十年历史就是
几 TB。`raw_dir` 的说明写着「restore 成功后可删」，但**直接删掉会让月库一旦丢失/
损坏就无法自愈**——见上一节「raw 清理后月库损坏」，那条路径除了人工重新走一遍
`download`（真实 CDS 请求、排队+带宽）别无他法。`archive` 要解决的正是这个：把
已经确认「下游用不到热盘也能自愈」的原始文件搬到冷盘，热盘腾出空间，冷盘留一份
兜底副本。

### 与服务器上 `forecast-coldmove.sh` 的关系

部署服务器（`climate_pipeline` 其它管线共用）上热盘是 `/training_data`、冷盘是
`/save_data`（44 TB），已有一个宿主机 shell 脚本 `forecast-coldmove.sh` 做同样
性质的事：判据是「下游产品已固化（`.zmetadata` 存在）才允许搬」，但校验只做到
「比对文件个数」。

`archive` 沿用同一条判据语义（下游已固化才搬），但**做成本项目自己的命令而不是
shell 脚本**——因为 manifest 里本来就记着每个原始文件的 sha256，可以逐文件比对
内容，而不是像那份脚本一样只数数量（数量对但某个文件已经悄悄损坏，coldmove 式
校验发现不了）。两者不共享实现，只是判据取向一致。

### 判据与搬运顺序

「下游已固化」在本项目里就是 `archivable()` 的判据：该 (月, 变量) 在月库里的
溯源 `manifest_sha256`，与该 manifest **当前**的 sha 相等——即 `restore` 确已
成功处理过这份 manifest，且 manifest 自身没有被后续改动（比如被 `refresh` 删掉
重下过）。不相等（月库溯源落后、或还没 restore 过）或压根没有溯源，一律不搬。

**先搬后删、验完再删**，顺序即安全性：

1. 拷贝到冷端的临时名（同目录，带 pid，同 `manifest.py` 写 manifest 的做法，
   避免并发归档进程互相踩踏）。
2. 算冷端副本的 sha256，与 **manifest 里记录的那个 sha256** 比对；不符 →
   删临时文件、跳过整份 manifest、告警，**绝不删热端**。一份 manifest 可能
   对应多个原始文件（如 `si10` = u10 + v10 两个源文件），任何一个文件校验不
   通过，这份 manifest 的全部文件都不动，不做部分搬运。
3. 全部文件校验通过后，才 `os.replace` 临时名 → 冷端正式名（同目录内原子
   操作）。
4. 确认冷端正式名就位（存在 + 内容 sha 仍正确）之后，才删热端。

任何一步失败都不许删热端——热端在归档这一刻是唯一已确认完整的副本，这个顺序
反了就是不可逆的数据丢失。

**绝不修改 manifest 文件的内容**：manifest 的 sha256 是 `restore` 幂等判断的
比对键，改它等于宣布对应月份需要重建。所以冷端位置**不写进 manifest**——
`restore._resolve` 找不到热端文件时，直接按 `cold_dir/<月份>/<原始文件名>`
去冷端找（拼路径逻辑是 `paths.cold_path`，`archive` 与 `restore` 都从这个
不依赖任何一方的公共模块 import，避免两边各自维护一份、或反过来互相依赖）；
两处都没有才报错，报错信息会提示下一步——确认数据确实不在了就删掉对应
manifest 触发重下。走冷端这条路径会打 `restore_cold_fallback` info 日志
点名月份和文件（不出声的话，"已归档所以走冷端"和"热端被误删"这两种情况在
日志里完全无法区分），并且会用 manifest 里记录的 sha256 校验冷端副本的
完整性——冷端副本往往是放了很久的最后一份兜底数据，恰恰是最该验的一份，
校验不通过就报错，不会把坏数据静默切进月库。`cold_dir` 为 `None`（未配置）
时，这条回退不生效，行为与引入本功能之前完全一致。

### 命令用法

形态照 `refresh`/`cleanup-jobs` 的范式：默认 dry-run，只有显式 `--yes` 才真正
搬运并清理热端。

```bash
uv run climate_reanalysis archive          # 默认 dry-run：只统计将搬走的文件数/跳过的 manifest 数，不动任何文件
uv run climate_reanalysis archive --yes    # 确认统计无误后，加 --yes 才真正搬运并清理热端
```

`cold_dir` 未在配置里显式指定（出厂 `None`）时，`archive` 拒绝执行并报错——
不给任何看似无害的默认路径（本机/多数开发环境根本没有那块盘），必须在
`config/settings.yaml` 里显式写清楚冷盘位置才能归档。服务器上按本仓库其它
管线的冷盘约定，示例配成 `/save_data/climate_data/era5/raw`。

### 冷盘挂载确认（哨兵文件）

`--yes` 真正执行前，除了要求 `cold_dir` 已配置，还要求这个目录**已经存在、
且下面有一个哨兵文件 `.climate_reanalysis_cold`**——缺一样都拒绝执行并报错
（预览模式不做这个检查，因为预览完全不碰文件系统）。

这不是多余的防呆：`cold_dir` 指向的挂载点在**没挂载**时，宿主机上通常表现为
一个存在、可写的空目录（挂载失败本身不报错，只是没有内容）。如果 `archive`
只检查"这个路径存不存在"，冷盘没挂上的时候就会在热盘（或根盘）上把整棵冷端
目录树建出来，逐文件拷贝、算 sha 校验（同一块盘，内容天然一致，校验永远
通过）、然后删掉热端——等真正的冷盘挂上，这份"冷端副本"被挂载点遮蔽，两边
都找不到，数据就丢了。哨兵文件是运维在**确认挂载之后**手动放的信号，纯代码
判断不出"挂载"这件事，只能靠这个替代：

```bash
# 确认冷盘已经挂载（df -h 或类似方式核实）之后，只需要做一次：
touch /save_data/climate_data/era5/raw/.climate_reanalysis_cold
```

这个哨兵文件本身不参与任何判据（不会被当成待归档的原始文件），只是一个存在性
标记；`cold_dir` 换了新盘/重新挂载后如果哨兵文件跟着丢了，需要重新 `touch` 一次。

### 月库丢失时的恢复路径

如果某个月库在归档**之后**又损坏/丢失（磁盘问题、误删 `.zarr` 目录等），此时
`raw_dir` 里对应的原始文件已经不在热盘上了——但不必因此重新发起真实 CDS 请求：

1. 确认受影响的 (月×变量)（同「raw 清理后月库损坏」一节，从 `restore` 的
   `restore_failed` 报错日志读）。
2. **不需要**删 manifest、也**不需要**跑 `download`——`restore`（或 `scan-once`）
   直接重跑即可：`load_raw` 发现热端文件缺失，自动回退到
   `cold_dir/<月份>/<原始文件名>` 读取原始数据，照常换算、写回月库。
3. 只有当冷端那份副本本身也不在/损坏（理论上不该发生：归档已经做过逐文件 sha
   校验才落盘）时，才需要退回「raw 清理后月库损坏」一节的人工重下流程。

这条自愈路径正是本功能存在的意义——冷存归档换来的不只是热盘空间，更是「月库
出问题时不必重新排队几十小时 CDS 请求」这件事本身。

## 历史导入（一次性烘焙）

`ssrd`/`fdir`/`t2m`/`si10`/`sp` 等变量的 240 个历史月库最初不是从本项目下载出来
的：早年 `solarcast` 项目按省下载了 20 年的省级 tile（`provinces_multiyear/<省
代码>.zarr`，供光伏发电量测算用，变量名随 pvlib、时间戳搬到区间中点 :30），
再用一套「烘焙」代码把这些 tile 拼成 ERA5 原生整点轴、ECMWF 变量名的月度国网库
——即本项目 `store.py` 的产出契约。

`bake-legacy` 子命令就是这套烘焙代码，从 `solarcast/scripts/bake_era5.py` +
`solarcast/src/solarcast/rebake/` 搬进 `src/climate_reanalysis/legacy_bake/`。
**这条路径以后用不上**：本项目的持续积累已经改为逐月直接从 CDS 下载
（`download` + `restore` 两个单元），不再需要省级 tile 这个中间形态；保留它
只是因为烘焙属于数据处理，而数据处理现在全部收拢在 `climate_reanalysis`
里——`solarcast` 以后只消费月度国网库，不再生产它。

```bash
# 冒烟（两个相邻省，有 buffer 重叠）
uv run climate_reanalysis bake-legacy --tiles /path/to/provinces_multiyear \
    --months 200501

# 全量（240 个月，分片优先遍历，账本可断点续跑；--out 留空 = settings.store_dir）
uv run climate_reanalysis bake-legacy --tiles data/provinces_multiyear \
    --months $(python3 -c "print(' '.join(f'{y}{m:02d}' for y in range(2005,2025) for m in range(1,13)))")

# 增量加变量（补下 u100/v100/d2m 之后，只跑新变量那一列）
uv run climate_reanalysis bake-legacy --tiles data/provinces_multiyear \
    --months 200501 --variables u100 v100 d2m
```

`--out` 留空时取 `settings.store_dir`——这正是收拢的意义：档案位置由项目配置
统一管，不必像原来的独立脚本那样每次手敲绝对路径。`--ledger` 同样留空时取
`<out>/_ledger.json`，记录 `(月, 变量, 分片)` 三元组的完成状态，中断后重跑会
跳过已完成的部分；补下新 tile（省份/变量）后账本按 tile 集合指纹自动整体
作废、全量重烘（避免补下的数据悄悄不进库、且不报错）。`bake-legacy` 全程只读
本地 tile、只写本地 zarr，不发起任何网络请求。

**网格常量不重复定义**：`legacy_bake/grid.py` 的 `TARGET_LAT`/`TARGET_LON`/
`SHARD`/`CHUNK` 直接从 `store.py` import（迁移前已用 `np.array_equal` 核对过
两处历史定义逐值相同），保证 `bake-legacy` 烘出来的网格与 `download` →
`restore` 那条路径产出的月库**逐格一致**——这两份原本独立维护的真值一旦漂移，
后果是网格对不上且没有任何报错，故收拢成一份，`tests/test_legacy_bake_grid.py`
有专门的一致性用例钉死。

**`bake-legacy` 与 `migrate` 的分工不同**：`bake-legacy` 从省级 tile 生成月库
的**实际数据**（写入 `ssrd`/`t2m` 等数组本身）；`migrate` 假定数据已经在库里
（不管是 `bake-legacy` 烘出来的，还是更早年用 solarcast 里的脚本烘的），只补
一条**溯源标记**，让 `restore` 认得这些月份已经就位、不会被误判成"从未处理"
而重新触发真实 CDS 下载。已经跑过一次历史烘焙（`.zarr` 已在 `store_dir` 下）
的机器上，通常只需要 `migrate`；只有月库本身缺失、需要从省级 tile 重新生成
时才需要 `bake-legacy`。

## 历史迁移（一次性）

现有历史月库（`solarcast` 一次性烘焙的 240 个月，`ssrd`/`fdir`/`t2m`/`si10`/`sp` 等
变量）经实测与本管线要用的 CDS 端点逐值相同（`array_equal` = True），无需重新下载——
只需打上 legacy 溯源，让 `restore` 认为它们已经就位：

```bash
uv run climate_reanalysis migrate \
  --months 200501 200502 200503 ... 202412 \
  --variables ssrd fdir t2m si10 sp \
  --tag legacy:solarcast-bake
```

`--months`/`--variables` 要传**显式列表**（本项目没有 `climate_download` 那种
`"20260501-20260507"` 区间简写语法）；大范围迁移可以用 shell/Python 生成月份列表再传入，
例如：

```bash
uv run climate_reanalysis migrate \
  --months $(python3 -c "print(' '.join(f'{y}{m:02d}' for y in range(2005,2025) for m in range(1,13)))") \
  --variables ssrd fdir t2m si10 sp
```

**`fdir` 例外**：历史段是 solarcast 路径的产物，经过 `bhi = min(fdir/3600, ghi)` 截断
再反解得到，与 CDS 原生 `fdir` 严格讲不同源。`migrate` 对它单独打
`legacy:solarcast-bake(clipped)` 标记，使这份差异在数据里可见、将来可选择重下
（其余变量不受影响）。

月库不存在时该月直接跳过（不会凭空建出一个只有标记、没有数据的库）；已经有真实
`manifest_sha256` 溯源的（月×变量）——即已经被这条管线正常下载 + restore 切割过
的——会被跳过并告警，不会覆盖：`migrate` 只补缺，不改写已有真实数据（范围传参很容易
跟已下载月份重叠，无条件覆盖会让"这份数据是怎么来的"这条线索消失且不报错）。对不存在
的月份、已有真实溯源的变量，`migrate` 都是安全的空操作。

**`migrate` 与 `download` 的判据互不相通，别指望一个知道另一个做了什么**：`migrate`
只看月库里的 `zrread_provenance`，`download`（经 manifest 文件）只看 `manifest_dir`
里有没有对应的 manifest。给一个月打了 legacy 标记**不会**在 `manifest_dir` 里留下
任何东西——所以 `download` 的 `--start`/`--months` 覆盖的范围**不要包含已经迁移过的
历史月份**，否则会把它们当成"从未下载"重新发起真实请求（这正是 `--start` 改成必填、
不给默认值的原因）。

## 挂定时任务

`restore` 是幂等的，重复跑安全，可以直接挂 cron。**必须加 `flock`**：`download` 单轮
可能跑十几小时（排队常 1–1.5 小时/请求，一批几十个变量顺序等下来很容易跨天），如果
cron 按"每天一次"这种周期挂，上一轮还没跑完下一轮就起来了——两个进程会对同一
(月×变量) 各提交一次真实 CDS 请求（重复占配额），还会同时 curl 写同一个 `.part`
文件互相踩踏。`restore` 单轮通常很快，但同样建议统一加锁，成本很低。服务器（无代理）：

```cron
30 6 * * * flock -n /tmp/climate_reanalysis_restore.lock -c "cd /path/to/climate_reanalysis && uv run climate_reanalysis restore >> /var/log/climate_reanalysis_restore.log 2>&1"
```

`download` 同样幂等（manifest 已存在的（月×变量）会跳过重复请求），但耗时长，不建议
塞进高频 cron；按月手动跑或用较宽松的周期（如每天一次，配合上面的 `flock` 防重入）。
**`--start` 必须显式给一个在已迁移历史范围之后的月份**（例如迁移覆盖到 202412，这里
就从 202501 起）——留空会报错，但传错了同样会把整段历史重新排队：

```cron
0 3 * * * flock -n /tmp/climate_reanalysis_download.lock -c "cd /path/to/climate_reanalysis && uv run climate_reanalysis download --start 202501 >> /var/log/climate_reanalysis_download.log 2>&1"
```

`-n`（`--nb`，non-blocking）在锁已被占用时直接放弃退出，不排队等待——比阻塞式加锁更
适合 cron：宁可这一轮不跑，也不要一堆等锁的进程越积越多。

本机长跑请用 `launchd` 而非会话级看护进程——`caffeinate -ims` 只挡空闲休眠，挡不住
合盖与电池休眠（会直接 SIGKILL 掉正在排队/下载的进程）。

## 幂等如何工作

月库的 `zrread_provenance[产出变量]` 记下产出它的 manifest 的 sha256（或 `migrate`
打的 `legacy:*` 标记）。`restore` 逐变量比对：sha 一致则跳过，不一致或缺失则只重建
**那一个变量**，其余变量与已有数据一字不动。

**状态存在产出里而非外部账本**——产出被删/被改，状态自然跟着变。外部账本会与现实
脱节：本项目的前身就出过一次（账本声称最新，实际漏了 14 个新数据块且不报错）。

## raw 清理后月库损坏：唯一不能自愈的路径，须手工恢复

**这一节针对的是 raw 被手工删除（`rm`）的情况**——如果 raw 是被 `archive` 命令
搬走的（而不是直接删除），恢复路径完全不同、也不需要人工介入，见上面「冷存
归档」一节的「月库丢失时的恢复路径」：`restore` 会自动从冷端读回来，不必重下。
本节说的"须手工恢复"，指的是原始文件已经彻底不存在（既不在热盘、也没有冷端
副本）的情况。

`raw_dir` 的说明写着"restore 成功后可删"——这在正常情况下没问题：`restore` 只在
manifest 的 sha 与月库溯源不一致时才需要读 raw，一致就跳过，不会碰 raw。但 manifest
**在**这件事，同时也是 `download` 判断"这个 (月×变量) 已经处理过、不用再下"的**唯一**
依据（见上面「manifest 存在即早退」这条闸门）——如果月库本身之后又损坏或丢失（磁盘
问题、误删某个月的 `.zarr`、诸如此类），而 raw 已经按建议清理掉了，就会卡进一个
**两头都拒绝、谁也没法自己修好**的状态：

- `restore` 每轮都会对这个 (月×变量) 报错（`load_raw` 找不到 raw 文件），退出码非 0；
- `download` 看到 manifest 还在，直接跳过、不重新请求——它没有办法知道月库那边已经坏了。

**恢复步骤**（确认月库确实丢失/损坏、需要重新走一遍下载后）：

1. 确认受影响的 (月×变量)——可以从 `restore` 的报错日志（`restore_failed`，带
   `manifest` 路径）里读到。
2. 删掉这些 (月×变量) 对应的 manifest 文件：`data/manifests/<YYYYMM>_<变量>.manifest.json`
   （单个 `rm`，或按月/变量批量写一段 shell/Python 删）。
3. 重新跑 `download --start <月份> --months <月份> --variables <变量>`——manifest 已经
   不在了，即使 raw 也不在，`download_one` 也会正常重新发起真实 CDS 请求、重新写 raw。
4. 再跑一次 `restore` 补齐月库。

这条路径本项目**不打算做成自动化**——「月库损坏」本身应当是罕见情况，自动删 manifest
去触发重下有误删的风险（比如把一次误报当成真损坏），不如留给人工确认后手动处理。

**「raw 缺失」与「月库本身物理损坏打不开」是两种不同的坏法，恢复方式不同**：上面
四步流程针对的是前者（月库结构完好，只是某个 (月×变量) 的溯源对不上、需要 raw 重建）。
如果是后者——月库的 `.zarr` 本身打不开（磁盘问题、`zarr.json` 截断/写坏等）——
`ensure_store`/`write_variable` 会抛 `StoreCorruptedError` 点名月份和路径，同样让
`restore` 报错退出非 0，**但绝不会自动删除或重建这个库**（这是刻意的：库里可能还有
其它变量的真实数据幸存，自动重建会把它们一并静默清空）。这种情况仅删 manifest 重跑
`download`/`restore` 不够——损坏的是月库文件本身，需要人工确认后手动删除/搬走这个
`<YYYY>/<YYYYMM>_era5.zarr` 目录，再按上面的步骤重新走一遍。另有一种中间状态：库能打开但
骨架不完整（建到一半崩溃）——若确认是空壳（无真实数据无溯源）会自动安全重建并打
`rebuilding_empty_skeleton` 日志；若已经写入过真实数据/溯源则同样拒绝重建，抛
`IncompleteStoreHasDataError`，需要人工核实后再决定重建还是修复。

## 目录结构

```
src/climate_reanalysis/
  settings.py    配置：路径、代理、并发、发布延迟、区域
  variables.py   产出变量 ↔ CDS 变量映射与单位换算
  period.py      月份枚举与「整月齐了才发布」的就绪判据
  manifest.py    manifest 读写——唯一的就绪信号
  cds.py         CDS 客户端与可续传下载（submit→poll→curl，避开会重排队的 retrieve()）
  store.py       月度国网库读写与溯源属性
  download.py    单元 A：按（月×产出变量）下载并写 manifest
  restore.py     单元 B：scan-once 幂等切库
  migrate.py     单元 C：历史迁移，打 legacy 溯源
  refresh.py     ERA5T 刷新：删过期月份的 manifest + 原始文件，交给 download+restore 自然重下 final
  archive.py     冷存归档：下游已固化才搬、逐文件 sha 校验、先搬后删、要求冷盘挂载哨兵
  paths.py       cold_path()：archive/restore/refresh 三边共用的冷端路径拼接，不依赖任何单元
  legacy_bake/   一次性历史导入：省级 tile → 月度国网库（搬自 solarcast/rebake，见「历史导入」一节）
    grid.py        目标网格常量（从 store.py import，不重复定义）、tile 落位、烘焙账本
    deconv.py      中点化（:30）反解回 ERA5 原生整点轴
    bake.py        遍历分片 × 变量，把 tile 数据换算、写入月库
  cli.py         参数 → 调用的翻译层，不含业务逻辑
scripts/         一次性运维脚本（不是常驻管线），如 migrate_to_year_layout.py
tests/           零真实网络依赖，全量 ~10s
```

## 明确不做

- 不做半个月发布——月末 + `publish_lag_days` 之前，该月不存在于任何输出里。
- 不引入外部账本——幂等状态只活在产出的溯源属性里。
- 不把 solarcast 省库当作长期输入——它只是一次性历史迁移的来源。
- 不做跨源准确性验证、HDD/CDD 派生量、气候基准表、把历史档案同步到服务器。

## 覆盖范围：两套变量的历史差异

档案里的九个变量来自**两条不同的路**，覆盖范围因此不一致——这是历史造成的，
不是缺陷，但读的时候要知道：

| 变量 | 来源 | 掩膜外（公海/境外）|
|---|---|---|
| `ssrd` `t2m` `si10` `sp` `fdir` | 早年 solarcast 的省级 tile 烘焙而来 | **NaN** |
| `u100` `v100` `d2m` `ssrdc` | 本项目从 CDS 按整个 `area` 直下 | **有真实值** |

省级 tile 只覆盖中国省域 + 沿海外扩补丁，所以烘出来的五个变量在公海和境外是
NaN；`coverage` 数组描述的正是这个范围（约占整个 box 的 63%）。后补的四个变量
没有经过这层裁剪。

**对实际使用没有影响**：省级池化、站点提取、沿海风机（外扩已含在 coverage 内）
都落在掩膜内，那里九个变量齐全。露出差异的只有公海与境外格点。

若将来希望档案单一来源、覆盖一致，做法是把那五个变量也从 CDS 直下一遍
（240 月 × 5 = 1200 次请求，约一天），届时 `coverage` 可以废弃、省级 tile 与
`bake-legacy` 彻底退役。当前刻意不做——收益是齐整，代价是一天下载，而异构只
存在于查询不到的地方。
