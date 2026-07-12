# share-quant

本项目是面向个人量化研究的 A 股本地数据库底座，首版只做数据同步、存储、校验和查询，不实现因子计算。

## 核心设计

- 数据源：Tushare Pro
- 存储：DuckDB catalog + Parquet 数据文件
- 范围：沪深京 A 股
- 默认区间：2021-01-01 至当前日期
- 凭证：从环境变量 `TUSHARE_TOKEN` 读取，不写入仓库

## 已覆盖的数据域

- 股票基础、公司资料、名称变更
- 日线行情、每日估值指标、复权因子、停复牌、涨跌停
- 资金流向、龙虎榜
- 利润表、资产负债表、现金流量表、财务指标
- 分红、前十大股东、前十大流通股东
- 指数基础、主要指数日线、主要指数成分权重
- 申万行业分类、申万行业成分

## 研究视图

- `v_adjusted_daily`：未复权行情、后复权行情、前复权行情。
- `v_adjusted_returns`：基于复权收盘价的日收益率。
- `v_stock_universe_daily`：按日股票池，包含上市日期、退市日期、上市状态、停牌状态和 ST 名称标记。
- `v_fina_indicator_asof_intervals`：财务指标公告可见区间。
- `v_income_asof_intervals`、`v_balancesheet_asof_intervals`、`v_cashflow_asof_intervals`：三大报表公告可见区间。
- `v_index_data`：指数基础、指数日线和指数权重统一视图。
- `v_industry_data`：行业分类和行业成分统一视图。

## 常用命令

```powershell
share-quant init-db
share-quant sync --dataset daily --start 2021-01-01 --end today
share-quant sync-all --start 2021-01-01 --end today
share-quant status
share-quant validate
```

如果当前 Python 的 `Scripts` 目录不在 `PATH` 中，也可以直接运行：

```powershell
python -m share_quant.cli init-db
python -m share_quant.cli status
python -m share_quant.cli validate
```

真实同步前请先设置 token：

```powershell
$env:TUSHARE_TOKEN = "your-token"
```

离线单元测试使用手写 fixture，不会调用真实 Tushare。

## 安全的全量分阶段同步

```powershell
share-quant sync-phased --start 2021-01-01 --end today --rate-limit-seconds 0.5 --pause-between-chunks 0.2
```

该命令会按数据集分组执行，按数据集配置拆分日期区间，默认从 `data/catalog/sync_phased_checkpoint.json` 断点续跑，失败 chunk 记录到 `data/catalog/sync_phased_progress.jsonl` 后跳过，并在每次 Tushare 请求前按限速等待。
