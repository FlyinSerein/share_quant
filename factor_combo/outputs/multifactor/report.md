# Multifactor Layered Backtest Report

## Sample and Method

- Output directory: `D:/share_quant/research/factor_combo/outputs/multifactor/`.
- Evaluation window: `20220101` to `20260707`; warmup starts at `20210101`.
- Input scores: industry and size neutralized single-factor scores.
- Composite factors: `Composite_Equal` and `Composite_RollingRankIC`.
- Rolling RankIC weights: window `12`, min periods `6`, negative means clipped to zero, fallback to equal weight when history is insufficient or non-positive.
- Minimum valid factor count per stock: `6`.
- Layers: `10` buckets, highest score is D10.
- Signal and execution: month-end signal, next trading day close execution, held until next rebalance.
- Transaction cost: one-way `0.10%` deducted by layer turnover.
- Benchmark: `000985.CSI`.

## Composite Coverage

| factor | signal_count | first_signal | last_signal | average_valid_stocks | min_valid_stocks | average_available_factor_count |
| --- | --- | --- | --- | --- | --- | --- |
| Composite_Equal | 54 | 20220128 | 20260630 | 4982.3704 | 4423.0000 | 10.2650 |
| Composite_RollingRankIC | 54 | 20220128 | 20260630 | 4981.4074 | 4423.0000 | 10.2650 |

## Top Layer Metrics

| factor | start_date | end_date | trading_days | annual_return | annual_volatility | max_drawdown | sharpe | cumulative_return | benchmark_annual_return | excess_annual_return | average_monthly_turnover | annualized_turnover | missing_return_count | suspended_return_count | invalid_missing_return_count | average_invalid_missing_weight | max_invalid_missing_weight | composite_factor | bucket |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Composite_RollingRankIC_D10 | 20220208 | 20260707 | 1070 | 11.89% | 21.99% | -28.30% | 0.5407 | 61.13% | 3.11% | 8.78% | 28.22% | 338.61% | 357 | 338 | 19 | 0.00% | 0.21% | Composite_RollingRankIC | 10 |
| Composite_Equal_D10 | 20220208 | 20260707 | 1070 | 8.73% | 25.20% | -31.20% | 0.3463 | 42.65% | 3.11% | 5.62% | 39.93% | 479.11% | 425 | 409 | 16 | 0.00% | 0.21% | Composite_Equal | 10 |

## D10 - D1 Long/Short Metrics

| composite_factor | period_count | annual_return | annual_volatility | sharpe | max_drawdown | win_rate | cumulative_return |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Composite_RollingRankIC | 54 | 4.82% | 15.66% | 0.3079 | -21.68% | 59.26% | 23.60% |
| Composite_Equal | 54 | -1.70% | 12.24% | -0.1390 | -20.36% | 44.44% | -7.43% |

## Composite RankIC

| composite_factor | period_count | ic_mean | ic_std | ic_win_rate | ic_monthly_icir | ic_annual_icir | rank_ic_mean | rank_ic_std | rank_ic_win_rate | rank_ic_monthly_icir | rank_ic_annual_icir | average_sample_count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Composite_RollingRankIC | 54 | 0.0224 | 0.1016 | 59.26% | 0.2204 | 0.7636 | 0.0569 | 0.1272 | 68.52% | 0.4469 | 1.5482 | 4980.1481 |
| Composite_Equal | 54 | -0.0014 | 0.0791 | 48.15% | -0.0179 | -0.0618 | -0.0053 | 0.0891 | 40.74% | -0.0595 | -0.2061 | 4981.1111 |

## Output Files

- Tables: `tables/composite_factor_weights.csv`, `tables/composite_scores_coverage.csv`, `tables/layer_metrics.csv`, `tables/layer_nav.csv`, `tables/layer_period_returns.csv`, `tables/long_short_metrics.csv`, `tables/top_layer_weights.csv`
- Extra diagnostics: `tables/composite_scores.csv`, `tables/layer_daily_returns.csv`, `tables/layer_turnover.csv`, `tables/composite_ic_by_period.csv`, `tables/composite_ic_summary.csv`
- Single-factor comparison: `tables/single_factor_top_metrics.csv`, `tables/single_factor_long_short_metrics.csv`

## Images

- `images/layer_period_returns.png`
- `images/long_short_nav.png`
- `images/single_vs_multifactor.png`
- `images/rankic_weights.png`
