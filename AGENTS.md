# share_quant workspace instructions

本目录是一个单仓库双项目工作区。

## 项目边界

- `database/` 只负责 Tushare 数据采集、DuckDB/Parquet 存储、同步、研究视图和数据质量校验。
- `factor_research/` 只负责因子构建、中性化、诊断、回测、组合和研究报告。
- 因子项目只能通过只读 DuckDB 连接使用数据库项目公开的研究视图或明确的只读 Parquet 数据。
- 禁止因子项目写入 `database/data/bronze`、`silver` 或 `catalog`。
- 修改数据库公开视图时，同时运行数据库测试和因子项目兼容性测试。

进入子项目工作时，继续遵守该目录内的 `AGENTS.md`。
