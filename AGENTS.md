# AGENTS.md

本文件用于指导 AI 编码代理在本仓库中进行因子构建、数据查询与回测相关开发。仓库当前核心能力是基于 Tushare Pro、DuckDB 和 Parquet 的本地 A 股研究数据库；新增因子或回测功能时，应优先复用现有数据层、研究视图和测试模式。

## 项目概览

- Python 包目录：`src/share_quant/`
- 配置文件：`configs/default.yaml`
- 本地数据库：`data/share_quant.duckdb`
- 原始分批数据：`data/bronze/<dataset>/*.parquet`
- 去重后的研究数据：`data/silver/<dataset>.parquet`
- 测试目录：`tests/`
- CLI 入口：`share-quant = share_quant.cli:main`

项目使用 DuckDB catalog 管理元数据和视图，使用 Parquet 保存实际数据。不要绕过 `StorageEngine` 随意改写 `data/silver`，除非任务明确要求修复数据文件，并且已经说明影响范围。

## 关键模块

- `src/share_quant/datasets.py`：所有数据集定义、主键、同步策略、日期字段、分组。
- `src/share_quant/storage.py`：DuckDB 初始化、bronze/silver 写入、去重 upsert、研究视图、数据校验。
- `src/share_quant/sync.py`：单数据集和全量同步逻辑。
- `src/share_quant/phased_sync.py`：分组、分块、可断点续跑同步。
- `src/share_quant/tushare_adapter.py`：Tushare API 适配层。
- `src/share_quant/cli.py`：命令行入口和参数定义。

新增研究能力时，先确认是否能通过现有视图完成；只有当视图缺失必要字段或语义时，再扩展 `storage.py` 中的研究视图。

## 数据集与研究视图

常用研究视图：

- `v_adjusted_daily`：日行情、复权因子、后复权价、前复权价。
- `v_adjusted_returns`：基于后复权收盘价的日收益率 `return_adjusted`。
- `v_stock_universe_daily`：逐日股票池，包含上市状态、停牌状态、ST 名称标记。
- `v_fina_indicator_asof_intervals`：财务指标公告可见区间。
- `v_income_asof_intervals`、`v_balancesheet_asof_intervals`、`v_cashflow_asof_intervals`：三大报表公告可见区间。
- `v_index_data`：指数基础、指数日线、指数权重合并视图。
- `v_industry_data`：申万行业分类和成分合并视图。

实际数据表不以 DuckDB base table 形式保存，通常应通过视图或 `read_parquet('data/silver/<dataset>.parquet')` 查询。

## 因子构建规则

- 日期字段在数据层通常是 `YYYYMMDD` 字符串；CLI 参数使用 `YYYY-MM-DD`。
- 行情类因子优先使用 `v_adjusted_daily` 或 `v_adjusted_returns`，避免自行拼接 `daily` 和 `adj_factor`。
- 股票池过滤优先使用 `v_stock_universe_daily`：
  - 排除未上市或已退市交易日：`is_listed_on_date = true`
  - 根据策略决定是否排除停牌：`is_suspended = false`
  - 根据策略决定是否排除 ST：`is_st_name = false`
- 财务类因子必须按公告可见时间做 as-of join，优先使用 `*_asof_intervals` 视图：
  - 只能在 `trade_date >= visible_from`
  - 如使用区间，限制 `trade_date < next_visible_from`，`next_visible_from is null` 表示当前最新可见记录
  - 不要用报告期 `end_date` 直接对齐交易日后向填充，这会引入未来函数
- 横截面因子应明确中性化、标准化、缺失值、极值处理规则；实现时把这些规则写成可测试的小函数。
- 避免在因子计算中隐式读取“当前最新”全量财务数据，除非任务明确是截面最新快照而非历史回测。

## 回测规则

- 默认假设信号在交易日收盘后生成，下一个可交易日成交；如果任务需要盘中或当日收盘成交，必须在代码和测试中显式说明。
- 收益率优先使用 `v_adjusted_returns.return_adjusted`；组合收益计算要清楚处理停牌、涨跌停、缺失价格和退市。
- 每次调仓都要用当时可见的股票池、行业、指数成分和财务数据。
- 基准优先从 `v_index_data` 中取主流指数日线，例如沪深 300、中证 500、中证 1000、全指。
- 不要把全样本统计量泄漏到历史截面中；滚动窗口、分位点、标准化参数都应只使用当期及以前数据。
- 回测输出至少包含：样本区间、股票池规则、调仓频率、交易成本假设、年化收益、波动率、最大回撤、Sharpe、换手率、基准及超额收益。

## 开发约定

- 优先保持现有轻量结构；新增功能可放在 `src/share_quant/factors.py`、`src/share_quant/backtest.py`，或在功能扩大后拆成 `factors/`、`backtest/` 包。
- 使用 DuckDB SQL 处理大表连接和窗口计算，使用 pandas 处理较小结果集和指标汇总。
- 对 SQL 标识符要考虑保留字和特殊列名；参考 `storage.py` 中的 `_quote()`。
- 不要提交 `.env`、Tushare token、临时 notebook 输出或大规模生成结果。
- 不要把 `data/bronze`、`data/silver`、`data/*.duckdb` 的大文件改动作为普通代码改动提交，除非任务明确是数据快照更新。
- 生成实验结果时优先写入 `data/research/` 或任务指定目录，并在 `.gitignore` 规则允许时避免纳入版本控制。

## 常用命令

初始化数据库：

```powershell
python -m share_quant.cli init-db
```

查看同步状态：

```powershell
python -m share_quant.cli status
```

运行数据校验：

```powershell
python -m share_quant.cli validate
```

同步单个数据集：

```powershell
python -m share_quant.cli sync --dataset daily --start 2021-01-01 --end today
```

分阶段同步：

```powershell
python -m share_quant.cli sync-phased --start 2021-01-01 --end today --rate-limit-seconds 0.5 --pause-between-chunks 0.2
```

运行测试：

```powershell
python -m pytest
```

真实同步前需要设置环境变量：

```powershell
$env:TUSHARE_TOKEN = "your-token"
```

## 测试要求

- 新增数据集、主键、同步策略时，补充 `tests/test_sync.py` 或相关测试。
- 修改存储、视图、校验逻辑时，补充 `tests/test_storage.py`。
- 新增因子函数时，使用小型手写 DataFrame fixture 验证：
  - 日期排序
  - 复权价格选择
  - 缺失值处理
  - 横截面排名或标准化
  - 是否避免未来数据
- 新增回测函数时，至少覆盖：
  - 信号日到成交日的滞后
  - 调仓权重归一化
  - 缺失收益率处理
  - 交易成本
  - 指标计算
- 测试不能依赖真实 Tushare 网络请求；沿用现有 fake adapter / fixture 风格。

## 数据质量检查

在依赖本地数据库进行研究前，先运行：

```powershell
python -m share_quant.cli validate
```

重点关注：

- `cross:daily_adj_factor` 是否通过。
- `cross:daily_trade_calendar` 是否通过。
- `view:v_adjusted_daily_row_count` 是否通过。
- 所需研究视图是否 queryable 且行数非零。

如果校验失败，不要直接继续做因子结论；先定位缺失的数据集或异常日期范围。

## 查询示例

示例：读取可交易股票池和复权收益。

```sql
select
    r.ts_code,
    r.trade_date,
    r.return_adjusted
from v_adjusted_returns r
join v_stock_universe_daily u
  on r.ts_code = u.ts_code
 and r.trade_date = u.trade_date
where u.is_listed_on_date = true
  and u.is_suspended = false
  and u.is_st_name = false
  and r.trade_date between '20210101' and '20211231';
```

示例：财务指标按公告日可见区间对齐交易日。

```sql
select
    d.ts_code,
    d.trade_date,
    f.roe,
    f.end_date,
    f.visible_from
from v_adjusted_daily d
join v_fina_indicator_asof_intervals f
  on d.ts_code = f.ts_code
 and d.trade_date >= f.visible_from
 and (f.next_visible_from is null or d.trade_date < f.next_visible_from);
```

## 完成任务前检查

- 代码是否复用现有 `DatasetSpec`、`StorageEngine`、研究视图和 CLI 约定。
- 是否避免未来函数和全样本泄漏。
- 是否没有无意改动本地大数据文件、`.env` 或用户已有改动。
- 是否运行了与改动范围匹配的测试；无法运行时说明原因。
- 涉及研究结论时，是否写明样本区间、股票池、调仓和交易成本假设。
