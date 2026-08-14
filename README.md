# climate_reanalysis

ERA5 再分析的**持续积累**管线，与 `climate_pipeline` 里的 `download → climate_restorage`
同构：按月下载、每（月 × 产出变量）一份 manifest 作为就绪信号、restore 幂等地把它切进
月度国网库。

与 `solarcast` 的省库/TMY **是两件事**：那是发电量收益测算资产，有自己的用途与生命
周期；二者仅在“历史数据复用、免于重复下载”这一点上有交集（见下方「历史迁移」），
此后不再耦合。

产出被 `climate_zrread` 的 `era5` 观测源直接消费（网格与 `ifs-hres` 逐格相同，
契约见 `docs/superpowers/specs/2026-08-13-climate-reanalysis-pipeline-design.md`）。

## 三个单元

| 命令 | 作用 |
|---|---|
| `download` | 每（月 × 产出变量）一次 CDS 请求 → `raw/` + `manifests/`（唯一的就绪信号） |
| `restore` | 幂等一趟扫描 manifest → 写进 `<YYYYMM>_era5.zarr`（可 cron） |
| `migrate` | 给既有月库的历史变量打 legacy 溯源，使 restore 认为已就位，避免重下 |

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
| `store_dir` | 月度国网库（`climate_zrread` 的 `era5` 源指向它） |
| `dl_proxy` | **只用于 bulk download**，submit/poll 一律直连；留空 = 裸连 |
| `concurrency` | 每期并发下载的（月×变量）任务数，出厂 4 |
| `publish_lag_days` | ERA5 发布延迟（天），出厂 5——月末再等这么多天才认为该月齐全 |
| `refresh_after_days` | ERA5T 初步数据换新窗口（天），出厂 100——月末再等这么多天才认为初步值「过期」，见下方「ERA5T 刷新」 |
| `area` | 请求区域 `(N, W, S, E)`，需与产出网格 15–55N / 70–140E 一致 |
| `cdsapirc` | 指定 `.cdsapirc` 路径；留空 = `~/.cdsapirc`（多账号轮换时可另起一份） |

`raw_dir`/`manifest_dir`/`store_dir` 的相对路径锚定到 `config/` 的**父目录**（即项目根），
不是运行时的 CWD——在哪个目录下敲 `uv run` 都一样。

**不要把 `dl_proxy` 或任何绝对路径硬编码进代码**：本机测试期有 Clash 代理（实测
4.8 MB/s），部署服务器当前无代理（裸连 CDS 仅 10.6 KB/s）——只应通过
`config/settings.yaml` 按机器区分。

`.cdsapirc` 格式（CDS 官方标准，`url`/`key` 两行）：

```
url: https://cds.climate.copernicus.eu/api
key: <your-key>
```

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
manifest 通信，这是刻意保持的契约——而是把过期月份对应的 manifest **删掉**，让既有
的 `download → restore` 流程自然把它们当成「没下过」重新走一遍：新下载产生新
manifest，sha 与月库溯源里记的旧 sha 天然对不上，`restore` 就会按幂等规则重建那个
变量。`refresh` 本身**不联网、不写任何新文件、不碰月库数据**，只做「找出过期月份」
和「删 manifest」两件事。

判据：该 (月, 变量) 的溯源 `expver` 里含 `"0005"`（哪怕只是跨边界月份的一部分），
且该月月末距今已超过 `refresh_after_days` 天（出厂 100，留出比 final 实测 2–3 个月
发布滞后更宽松的余量）。**缺 `expver` 键的老条目一律视为不需要刷新**——`migrate`
打的 legacy 标记、更早版本写的溯源都没有这个字段，宁可漏刷，也不要把没记录这个
字段的历史数据（可能是几十 GB、几十小时排队量级）全部当成待刷新对象重下。

```bash
uv run climate_reanalysis refresh                              # 默认 dry-run：只打印清单，不删
uv run climate_reanalysis refresh --after-days 60               # 覆盖配置里的 refresh_after_days
uv run climate_reanalysis refresh --today 2026-06-01             # 覆盖“今天”，便于核对判据
uv run climate_reanalysis refresh --yes                          # 确认清单无误后，加 --yes 才真删
```

**不加 `--yes` 是默认行为、不删任何文件**——删 manifest 会触发真实重下，代价是排队
时间和带宽，必须先看清楚清单再决定。**`refresh --yes` 执行完之后，必须再手动跑一次
`download` + `restore`**（`refresh` 自己不做这两步）：manifest 没了之后，`download`
才会把这些 (月, 变量) 当成「没下过」重新发起真实 CDS 请求，`restore` 再把新结果
（连带新的 `expver`）切回月库，完成真正的换新。这条通路与「raw 清理后月库损坏」
一节里的手工恢复步骤是同一套底层机制（删 manifest → 触发 sha 不符 → 自然重建），
`refresh` 只是把它包装成一个显式、可预览、按 ERA5T 判据自动找出目标的命令。

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
`<YYYYMM>_era5.zarr` 目录，再按上面的步骤重新走一遍。另有一种中间状态：库能打开但
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
  refresh.py     ERA5T 刷新：删过期月份的 manifest，交给 download+restore 自然重下 final
  cli.py         参数 → 调用的翻译层，不含业务逻辑
tests/           零真实网络依赖，全量 ~10s
```

## 明确不做

- 不做半个月发布——月末 + `publish_lag_days` 之前，该月不存在于任何输出里。
- 不引入外部账本——幂等状态只活在产出的溯源属性里。
- 不把 solarcast 省库当作长期输入——它只是一次性历史迁移的来源。
- 不做跨源准确性验证、HDD/CDD 派生量、气候基准表、把历史档案同步到服务器。
