# AGENTS.md

本文件指导 AI 编码代理维护 `database/` 数据库子项目。本项目只负责构建和维护本地 A 股研究数据库，不负责因子构建、组合回测或研究报告；这些功能属于相邻的 `factor_research/` 项目。

## 项目职责

- 数据源：Tushare Pro。
- 存储：DuckDB catalog + Parquet 数据文件。
- 功能：数据集定义、同步、去重、研究视图、状态管理和数据质量校验。
- Python 包：`src/share_quant/`。
- 配置：`configs/default.yaml`。
- 本地数据库：`data/share_quant.duckdb`。
- 原始数据：`data/bronze/<dataset>/*.parquet`。
- 研究数据：`data/silver/<dataset>.parquet`。
- 测试：`tests/`。
- CLI：`share-quant = share_quant.cli:main`。

禁止在本项目中新增因子、中性化、投资组合、回测、绩效分析或报告生成代码。因子项目只能通过只读连接使用本项目公开的数据接口。

## 关键模块

- `src/share_quant/datasets.py`：数据集定义、主键、同步策略、日期字段和分组。
- `src/share_quant/storage.py`：DuckDB 初始化、bronze/silver 写入、去重 upsert、研究视图和数据校验。
- `src/share_quant/sync.py`：单数据集和全量同步逻辑。
- `src/share_quant/phased_sync.py`：分组、分块和断点续跑同步。
- `src/share_quant/tushare_adapter.py`：Tushare API 适配层。
- `src/share_quant/cli.py`：命令行入口和参数定义。

不要绕过 `StorageEngine` 随意改写 `data/silver`。只有任务明确要求修复数据文件并说明影响范围时，才允许直接处理数据文件。

## 公共数据接口

以下 DuckDB 研究视图是提供给 `factor_research/` 的公共只读接口：

- `v_adjusted_daily`
- `v_adjusted_returns`
- `v_stock_universe_daily`
- `v_fina_indicator_asof_intervals`
- `v_income_asof_intervals`
- `v_balancesheet_asof_intervals`
- `v_cashflow_asof_intervals`
- `v_index_data`
- `v_industry_data`

修改这些视图时必须：

1. 保持现有字段名称和语义兼容，除非任务明确要求破坏性变更。
2. 明确日期格式、主键、空值和公告可见性语义。
3. 补充 `tests/test_storage.py` 测试。
4. 同时运行相邻 `factor_research/` 的测试，验证消费者兼容性。

财务历史数据必须以公告可见时间为准。`*_asof_intervals` 应保证 `visible_from` 和 `next_visible_from` 区间语义正确，不能使用报告期直接后向填充交易日而引入未来数据。

## 开发规则

- 新增数据集时优先复用 `DatasetSpec`、现有同步流程和 bronze/silver upsert 模式。
- 大表连接、窗口计算和视图构建优先使用 DuckDB SQL。
- SQL 标识符必须考虑保留字和特殊列名，参考 `storage.py` 中的 `_quote()`。
- 日期字段在数据层通常使用 `YYYYMMDD` 字符串；CLI 参数使用 `YYYY-MM-DD`。
- 测试不得请求真实 Tushare 网络，沿用 fake adapter 和手写 fixture。
- 不提交 `.env`、Tushare token、`data/` 大文件、临时数据库、缓存或生成结果。
- 不得无意修改 `data/bronze`、`data/silver`、`data/catalog`、`data/quarantine` 或 `data/*.duckdb`。
- 修改公开视图时优先兼容已有消费者；确需变更接口时，应同步修改文档、测试和 `factor_research/` 调用。

## 常用命令

以下命令均从 `database/` 目录运行：

```powershell
python -m share_quant.cli init-db
python -m share_quant.cli status
python -m share_quant.cli validate
python -m share_quant.cli sync --dataset daily --start 2021-01-01 --end today
python -m share_quant.cli sync-phased --start 2021-01-01 --end today --rate-limit-seconds 0.5 --pause-between-chunks 0.2
python -m pytest
```

真实同步前由用户在本地设置环境变量：

```powershell
$env:TUSHARE_TOKEN = "your-token"
```

不要读取、输出或提交 token 内容。

## 测试要求

- 新增或修改数据集、主键、同步策略：补充 `tests/test_sync.py` 或相关同步测试。
- 修改存储、去重、视图或校验逻辑：补充 `tests/test_storage.py`。
- 修改 CLI：补充 `tests/test_cli.py`。
- 修改分阶段同步：补充 `tests/test_phased_sync.py`。
- 测试应覆盖正常路径、空数据、重复数据、日期边界和失败恢复等相关场景。

## 数据质量检查

依赖本地数据库开展研究前，应运行：

```powershell
python -m share_quant.cli validate
```

重点确认：

- `cross:daily_adj_factor` 通过。
- `cross:daily_trade_calendar` 通过。
- `view:v_adjusted_daily_row_count` 通过。
- 所有公共研究视图可查询且必要视图行数非零。

校验失败时先定位缺失数据集、异常日期或视图路径，不应在数据质量未知时向因子项目提供研究结论。

## 完成任务前检查

- 是否严格保持数据库项目职责，没有混入因子或回测代码。
- 是否复用了 `DatasetSpec`、`StorageEngine`、同步流程和研究视图约定。
- 是否保持公共视图兼容并避免未来数据。
- 是否没有无意改动大数据文件、`.env` 或用户已有改动。
- 是否运行了与改动范围匹配的数据库测试。
- 涉及公共接口时，是否运行了 `factor_research/` 兼容性测试。
