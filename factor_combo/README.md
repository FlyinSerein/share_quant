# Excel 单因子回测子项目

本目录用于复现 `因子组合(1).xls` 中 11 个单因子的构建与月度 Top20% 回测。脚本只读访问项目已有数据库与 parquet，不会写入 `data/`。

## 运行

```powershell
python research\factor_combo\run_factor_backtest.py
```

默认输出全部写入：

```text
research/factor_combo/outputs/
```

可选参数：

```powershell
python research\factor_combo\run_factor_backtest.py --start 2022-01-01 --end 2026-07-07 --transaction-cost 0.001
```

## 回测口径

- 评价期默认从 `2022-01-01` 开始，2021 年数据只用于 60 日动量和 252 日波动率预热。
- 每月最后交易日收盘后打分，下一交易日收盘成交。
- 每个单因子持有截面得分前 20% 股票，等权配置。
- 股票池要求已上市、未停牌、非 ST。
- 基准为中证全指 `000985.CSI`。
- 单边交易成本默认 10bp，按调仓换手扣除。
- 财务类和股东类因子按公告可见时间 as-of 对齐，避免未来函数。

## 输出

- `outputs/report.md`：中文研究报告。
- `outputs/tables/factor_metrics.csv`：核心绩效指标。
- `outputs/tables/factor_coverage.csv`：因子覆盖率。
- `outputs/tables/nav_by_factor.csv`：净值曲线。
- `outputs/tables/monthly_returns.csv`：月度收益。
- `outputs/tables/turnover.csv`：每次调仓换手。
- `outputs/tables/rebalance_weights.csv`：每次调仓持仓权重。
- `outputs/images/*.png`：净值、回撤和表格图像。
