# Industry and Size Neutralized Top20% Backtest Report

## Sample and Method

- Data source: read-only `data/share_quant.duckdb`, existing research views, and silver parquet.
- Output directory: `D:/share_quant/research/factor_combo/outputs/neutralized/`.
- Evaluation window: `20220101` to `20260707`; warmup starts at `20210101`.
- Signal and execution: month-end close signal, next trading day close execution, held until the next rebalance.
- Universe: listed, not suspended, and non-ST stocks at the execution date.
- Benchmark: `000985.CSI`, valid benchmark return days `1332`.
- Transaction cost: one-way `0.10%`, deducted by rebalance turnover.
- Neutralization: after the existing winsorization and z-score step, regress `score` on `log(total_mv)` and current first-level industry dummies within each `factor + trade_date`; use only the residual `neutralized_score` for Top20% selection.
- Missing exposures: missing industry is `Unknown`; missing `log(total_mv)` is filled by the same cross-section mean, or 0 if the cross-section has no valid size.

## Neutralized Metrics

| factor | annual_return | annual_volatility | max_drawdown | sharpe | excess_annual_return |
| --- | --- | --- | --- | --- | --- |
| PE TTM | 19.30% | 26.45% | -30.72% | 0.73 | 16.19% |
| Volatility | 15.08% | 22.00% | -30.91% | 0.69 | 11.97% |
| Dividend Yield | 14.47% | 21.13% | -23.34% | 0.68 | 11.36% |
| Revenue Growth | 13.88% | 26.16% | -34.86% | 0.53 | 10.77% |
| Debt/Equity | 11.63% | 24.28% | -29.26% | 0.48 | 8.52% |
| Holder Concentration | 10.96% | 24.63% | -29.57% | 0.44 | 7.85% |
| Gross Margin | 10.75% | 25.27% | -32.02% | 0.43 | 7.64% |
| ROE | 10.69% | 23.93% | -27.46% | 0.45 | 7.58% |
| Momentum 60D | 8.05% | 26.46% | -36.63% | 0.30 | 4.94% |
| Main Net In | 5.90% | 23.94% | -35.30% | 0.25 | 2.79% |
| Turnover 20D | 3.26% | 29.35% | -42.91% | 0.11 | 0.15% |

## Raw vs Neutralized Comparison

| factor | annual_return_raw | annual_return_neutralized | annual_return_delta | excess_annual_return_delta | sharpe_delta | max_drawdown_delta |
| --- | --- | --- | --- | --- | --- | --- |
| PE TTM | 9.47% | 19.30% | 9.83% | 9.83% | 0.23 | -12.81% |
| Volatility | 9.13% | 15.08% | 5.95% | 5.95% | 0.19 | -8.47% |
| ROE | 6.29% | 10.69% | 4.40% | 4.40% | 0.16 | 6.22% |
| Main Net In | 1.88% | 5.90% | 4.02% | 4.02% | 0.17 | 4.46% |
| Gross Margin | 7.96% | 10.75% | 2.80% | 2.80% | 0.11 | 2.00% |
| Dividend Yield | 12.05% | 14.47% | 2.42% | 2.42% | 0.07 | -3.89% |
| Momentum 60D | 6.82% | 8.05% | 1.23% | 1.23% | 0.06 | 6.48% |
| Holder Concentration | 10.00% | 10.96% | 0.96% | 0.96% | 0.03 | -0.84% |
| Debt/Equity | 11.50% | 11.63% | 0.13% | 0.13% | 0.06 | 5.20% |
| Turnover 20D | 3.67% | 3.26% | -0.41% | -0.41% | -0.01 | 1.51% |
| Revenue Growth | 14.49% | 13.88% | -0.61% | -0.61% | -0.02 | 4.23% |

## Coverage

| factor | signal_count | first_signal | last_signal | valid_rows | average_holding_count |
| --- | --- | --- | --- | --- | --- |
| Debt/Equity | 54 | 2022-01-28 | 2026-06-30 | 269210 | 997 |
| Dividend Yield | 54 | 2022-01-28 | 2026-06-30 | 188605 | 699 |
| Gross Margin | 54 | 2022-01-28 | 2026-06-30 | 264069 | 978 |
| Holder Concentration | 54 | 2022-01-28 | 2026-06-30 | 245755 | 910 |
| Main Net In | 54 | 2022-01-28 | 2026-06-30 | 257598 | 954 |
| Momentum 60D | 54 | 2022-01-28 | 2026-06-30 | 266515 | 987 |
| PE TTM | 54 | 2022-01-28 | 2026-06-30 | 211733 | 784 |
| ROE | 54 | 2022-01-28 | 2026-06-30 | 269229 | 997 |
| Revenue Growth | 54 | 2022-01-28 | 2026-06-30 | 269240 | 997 |
| Turnover 20D | 54 | 2022-01-28 | 2026-06-30 | 268307 | 993 |
| Volatility | 54 | 2022-01-28 | 2026-06-30 | 255032 | 944 |

## Quick Read

- Best raw annual return factor in the paired run: `Revenue_Growth`.
- Best neutralized annual return factor: `PE_TTM`.
- Raw and neutralized runs use the same sample window, universe filter, rebalance calendar, transaction cost, and benchmark.

## Output Files

- Tables: `tables/factor_metrics.csv`, `tables/factor_coverage.csv`, `tables/nav_by_factor.csv`, `tables/monthly_returns.csv`, `tables/turnover.csv`, `tables/rebalance_weights.csv`, `tables/factor_metrics_comparison.csv`
- Images: `images/nav_curve.png`, `images/drawdown.png`, `images/metrics_table.png`, `images/metrics_comparison_table.png`, `images/raw_vs_neutralized_annual_return.png`

## Images

- `images/nav_curve.png`
- `images/drawdown.png`
- `images/metrics_table.png`
- `images/coverage_table.png`
- `images/metrics_comparison_table.png`
- `images/raw_vs_neutralized_annual_return.png`
