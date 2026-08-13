# climate_reanalysis

`climate_pipeline` 单体仓库的 ERA5 再分析持续积累子项目。

## 快速开始

```bash
uv sync
uv run pytest -q
```

## 配置

复制 `config/settings.yaml.example` 为 `config/settings.yaml` 后按机器修改：

- `raw_dir`: 原始 netCDF 落盘（restore 成功后可删）
- `manifest_dir`: manifest 目录——唯一的就绪信号
- `store_dir`: 月度国网库（zrread 的 era5 源指向它）
- `dl_proxy`: bulk download 走的代理；留空 = 裸连
- `concurrency`: 每期并发下载的变量数
- `publish_lag_days`: ERA5 发布延迟（天）
- `area`: 请求区域 (N, W, S, E)
- `cdsapirc`: 指定 .cdsapirc 路径；留空 = ~/.cdsapirc
