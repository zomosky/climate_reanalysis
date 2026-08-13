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

## 两个单元怎么调

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

`--config` 是顶层参数，写在子命令**之前**：`climate_reanalysis --config path/to/settings.yaml download ...`；
不传则用相对 CWD 的 `config/settings.yaml`。

`--start` **必填、没有默认值**（哪怕搭配 `--months` 显式指定月份也要传，argparse 不看
业务逻辑）——这是有意的：见下方「历史迁移」一节末尾的说明，一个"看起来无害"的默认值
会在配合 `migrate` 使用时把整段历史重新引向真实网络请求。

`download` 未就绪时不联网、不报错，直接跳过并返回退出码 0（同月其它变量互不影响）；
`restore` 全部成功退出码 0，有已就绪的 manifest 处理失败则非 0（同 `climate_restorage`
的 `scan-once`）。

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

`restore` 是幂等的，重复跑安全，可以直接挂 cron。服务器（无代理）：

```cron
30 6 * * * cd /path/to/climate_reanalysis && uv run climate_reanalysis restore >> /var/log/climate_reanalysis_restore.log 2>&1
```

`download` 同样幂等（manifest 已存在的（月×变量）会跳过重复请求），但耗时长（排队常
1–1.5 小时/请求），不建议塞进高频 cron；按月手动跑或用较宽松的周期（如每天一次）。
**`--start` 必须显式给一个在已迁移历史范围之后的月份**（例如迁移覆盖到 202412，这里
就从 202501 起）——留空会报错，但传错了同样会把整段历史重新排队：

```cron
0 3 * * * cd /path/to/climate_reanalysis && uv run climate_reanalysis download --start 202501 >> /var/log/climate_reanalysis_download.log 2>&1
```

本机长跑请用 `launchd` 而非会话级看护进程——`caffeinate -ims` 只挡空闲休眠，挡不住
合盖与电池休眠（会直接 SIGKILL 掉正在排队/下载的进程）。

## 幂等如何工作

月库的 `zrread_provenance[产出变量]` 记下产出它的 manifest 的 sha256（或 `migrate`
打的 `legacy:*` 标记）。`restore` 逐变量比对：sha 一致则跳过，不一致或缺失则只重建
**那一个变量**，其余变量与已有数据一字不动。

**状态存在产出里而非外部账本**——产出被删/被改，状态自然跟着变。外部账本会与现实
脱节：本项目的前身就出过一次（账本声称最新，实际漏了 14 个新数据块且不报错）。

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
  cli.py         参数 → 调用的翻译层，不含业务逻辑
tests/           零真实网络依赖，全量 ~10s
```

## 明确不做

- 不做半个月发布——月末 + `publish_lag_days` 之前，该月不存在于任何输出里。
- 不引入外部账本——幂等状态只活在产出的溯源属性里。
- 不把 solarcast 省库当作长期输入——它只是一次性历史迁移的来源。
- 不做跨源准确性验证、HDD/CDD 派生量、气候基准表、把历史档案同步到服务器。
