# Walk-Forward Champion vs Multifactor Report

## Protocol

- Run ID: `wf_70bd8295acfc`
- Stage-one artifact: `stage1_accepted_baselines_v1` (`f808a19a93b5864e046efe91789c7e02d75f9aac6bfbd429ca6cc88bb1e03715`)
- Window: `24` training + `12` validation + `6` OOS periods; step `6`.
- Champion rule: validation net D10-D1 monthly Sharpe; challenger margin `0.10`.
- Signal/execution: month-end signal, next-trading-day execution; only completed holding periods are available to decisions.
- Transaction cost: one-way `0.10%` on both long/short legs.
- Benchmark: `000985.CSI`.

## Completed Folds

| run_id | fold_id | status | decision_signal_date | train_start | train_end | validation_start | validation_end | embargo_signal_date | oos_start | oos_end | oos_completed_periods |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| wf_70bd8295acfc | F001 | complete | 20250228 | 20220128 | 20231229 | 20240131 | 20241231 | 20250127 | 20250228 | 20250731 | 6.0000 |
| wf_70bd8295acfc | F002 | complete | 20250829 | 20220729 | 20240628 | 20240731 | 20250630 | 20250731 | 20250829 | 20260130 | 6.0000 |

## Champion History

| fold_id | factor_id | fold_decision_reason |
| --- | --- | --- |
| F001 | PE_TTM | initial_champion |
| F002 | PE_TTM | incumbent_remains_best |

## Pooled Out-of-Sample Metrics

| run_id | scope | fold_id | strategy_id | portfolio_leg | metric_frequency | start_date | end_date | observation_count | annual_return | annual_volatility | sharpe | max_drawdown | win_rate | cumulative_return | average_period_turnover | total_transaction_cost | benchmark_annual_return | excess_annual_return | information_ratio | rank_ic_mean | rank_ic_periods |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| wf_70bd8295acfc | pooled | ALL | Benchmark_000985_CSI | benchmark | daily | 20250304 | 20260302 | 241.0000 | 0.3302 | 0.1729 | 1.7388 | -0.1375 | 0.5602 | 0.3137 | 0.0000 | 0.0000 | 0.3302 | 0.0000 | <NA> | <NA> | 0.0000 |
| wf_70bd8295acfc | pooled | ALL | Champion_SingleFactor | d1 | daily | 20250304 | 20260302 | 241.0000 | 0.3624 | 0.2387 | 1.4197 | -0.1991 | 0.5851 | 0.3441 | 0.2555 | 0.0031 | 0.3302 | 0.0322 | 0.36426777906792 | 0.035998541938893826 | 12.0000 |
| wf_70bd8295acfc | pooled | ALL | Composite_Equal | d1 | daily | 20250304 | 20260302 | 241.0000 | 0.3398 | 0.2224 | 1.4304 | -0.1962 | 0.5934 | 0.3228 | 0.3576 | 0.0043 | 0.3302 | 0.0096 | 0.17225367252489734 | 0.0015146933655301852 | 12.0000 |
| wf_70bd8295acfc | pooled | ALL | Composite_RollingRankIC | d1 | daily | 20250304 | 20260302 | 241.0000 | 0.2701 | 0.2482 | 1.0920 | -0.2239 | 0.5726 | 0.2569 | 0.3007 | 0.0036 | 0.3302 | -0.0601 | -0.24853029475118468 | 0.06898938562879654 | 12.0000 |
| wf_70bd8295acfc | pooled | ALL | Champion_SingleFactor | d10 | daily | 20250304 | 20260302 | 241.0000 | 0.4510 | 0.2333 | 1.7174 | -0.2006 | 0.5851 | 0.4276 | 0.2229 | 0.0027 | 0.3302 | 0.1208 | 0.9513615085725763 | 0.035998541938893826 | 12.0000 |
| wf_70bd8295acfc | pooled | ALL | Composite_Equal | d10 | daily | 20250304 | 20260302 | 241.0000 | 0.4303 | 0.2222 | 1.7265 | -0.1839 | 0.5809 | 0.4081 | 0.4471 | 0.0054 | 0.3302 | 0.1001 | 0.9241695902013456 | 0.0015146933655301852 | 12.0000 |
| wf_70bd8295acfc | pooled | ALL | Composite_RollingRankIC | d10 | daily | 20250304 | 20260302 | 241.0000 | 0.4338 | 0.1775 | 2.1232 | -0.1479 | 0.6266 | 0.4115 | 0.2983 | 0.0036 | 0.3302 | 0.1037 | 0.909763129357022 | 0.06898938562879654 | 12.0000 |
| wf_70bd8295acfc | pooled | ALL | Champion_SingleFactor | long_short | monthly | 20250304 | 20260302 | 12.0000 | 0.0527 | 0.0433 | 1.2117 | -0.0172 | 0.6667 | 0.0527 | 0.4784 | 0.0057 | 0.0000 | 0.0527 | 0.9758738255310626 | 0.035998541938893826 | 12.0000 |
| wf_70bd8295acfc | pooled | ALL | Composite_Equal | long_short | monthly | 20250304 | 20260302 | 12.0000 | 0.0527 | 0.0678 | 0.7930 | -0.0505 | 0.4167 | 0.0527 | 0.8047 | 0.0097 | 0.0000 | 0.0527 | 0.7576053862889155 | 0.0015146933655301852 | 12.0000 |
| wf_70bd8295acfc | pooled | ALL | Composite_RollingRankIC | long_short | monthly | 20250304 | 20260302 | 12.0000 | 0.0923 | 0.1040 | 0.9030 | -0.0454 | 0.5000 | 0.0923 | 0.5990 | 0.0072 | 0.0000 | 0.0923 | 0.8973735366701846 | 0.06898938562879654 | 12.0000 |
| wf_70bd8295acfc | pooled | ALL | Champion_SingleFactor | top20 | daily | 20250304 | 20260302 | 241.0000 | 0.4501 | 0.2159 | 1.8340 | -0.1836 | 0.5851 | 0.4268 | 0.2172 | 0.0026 | 0.3302 | 0.1199 | 1.0439248864671231 | 0.035998541938893826 | 12.0000 |
| wf_70bd8295acfc | pooled | ALL | Composite_Equal | top20 | daily | 20250304 | 20260302 | 241.0000 | 0.4506 | 0.2095 | 1.8847 | -0.1721 | 0.5892 | 0.4272 | 0.3639 | 0.0044 | 0.3302 | 0.1204 | 1.1757342905384562 | 0.0015146933655301852 | 12.0000 |
| wf_70bd8295acfc | pooled | ALL | Composite_RollingRankIC | top20 | daily | 20250304 | 20260302 | 241.0000 | 0.4361 | 0.1806 | 2.0985 | -0.1481 | 0.6224 | 0.4136 | 0.2485 | 0.0030 | 0.3302 | 0.1059 | 0.9579952673986236 | 0.06898938562879654 | 12.0000 |

## Files

- Tables: `tables/fold_schedule.csv`, `selection_log.csv`, `factor_weights.csv`, `oos_daily_returns.csv`, `oos_metrics.csv`
- Images: `images/oos_nav.png`, `fold_comparison.png`, `champion_history.png`
- Export: `exports/walk_forward_champion.zip`
