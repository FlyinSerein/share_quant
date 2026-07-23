# Walk-Forward Champion vs Multifactor Report

## Protocol

- Run ID: `wf_cd46c725e308`
- Stage-one artifact: `stage1_accepted_baselines_v1` (`f808a19a93b5864e046efe91789c7e02d75f9aac6bfbd429ca6cc88bb1e03715`)
- Window: `24` training + `12` validation + `1` OOS periods; step `1`.
- Champion rule: validation net D10-D1 monthly Sharpe; challenger margin `0.10`.
- Signal/execution: month-end signal, next-trading-day execution; only completed holding periods are available to decisions.
- Transaction cost: one-way `0.10%` on both long/short legs.
- Benchmark: `000985.CSI`.

## Completed Folds

| run_id | fold_id | status | decision_signal_date | train_start | train_end | validation_start | validation_end | embargo_signal_date | oos_start | oos_end | oos_completed_periods |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| wf_cd46c725e308 | F001 | complete | 20250228 | 20220128 | 20231229 | 20240131 | 20241231 | 20250127 | 20250228 | 20250228 | 1.0000 |
| wf_cd46c725e308 | F002 | complete | 20250331 | 20220228 | 20240131 | 20240229 | 20250127 | 20250228 | 20250331 | 20250331 | 1.0000 |
| wf_cd46c725e308 | F003 | complete | 20250430 | 20220331 | 20240229 | 20240329 | 20250228 | 20250331 | 20250430 | 20250430 | 1.0000 |
| wf_cd46c725e308 | F004 | complete | 20250530 | 20220429 | 20240329 | 20240430 | 20250331 | 20250430 | 20250530 | 20250530 | 1.0000 |
| wf_cd46c725e308 | F005 | complete | 20250630 | 20220531 | 20240430 | 20240531 | 20250430 | 20250530 | 20250630 | 20250630 | 1.0000 |
| wf_cd46c725e308 | F006 | complete | 20250731 | 20220630 | 20240531 | 20240628 | 20250530 | 20250630 | 20250731 | 20250731 | 1.0000 |
| wf_cd46c725e308 | F007 | complete | 20250829 | 20220729 | 20240628 | 20240731 | 20250630 | 20250731 | 20250829 | 20250829 | 1.0000 |
| wf_cd46c725e308 | F008 | complete | 20250930 | 20220831 | 20240731 | 20240830 | 20250731 | 20250829 | 20250930 | 20250930 | 1.0000 |
| wf_cd46c725e308 | F009 | complete | 20251031 | 20220930 | 20240830 | 20240930 | 20250829 | 20250930 | 20251031 | 20251031 | 1.0000 |
| wf_cd46c725e308 | F010 | complete | 20251128 | 20221031 | 20240930 | 20241031 | 20250930 | 20251031 | 20251128 | 20251128 | 1.0000 |
| wf_cd46c725e308 | F011 | complete | 20251231 | 20221130 | 20241031 | 20241129 | 20251031 | 20251128 | 20251231 | 20251231 | 1.0000 |
| wf_cd46c725e308 | F012 | complete | 20260130 | 20221230 | 20241129 | 20241231 | 20251128 | 20251231 | 20260130 | 20260130 | 1.0000 |
| wf_cd46c725e308 | F013 | complete | 20260227 | 20230131 | 20241231 | 20250127 | 20251231 | 20260130 | 20260227 | 20260227 | 1.0000 |
| wf_cd46c725e308 | F014 | complete | 20260331 | 20230228 | 20250127 | 20250228 | 20260130 | 20260227 | 20260331 | 20260331 | 1.0000 |
| wf_cd46c725e308 | F015 | complete | 20260430 | 20230331 | 20250228 | 20250331 | 20260227 | 20260331 | 20260430 | 20260430 | 1.0000 |
| wf_cd46c725e308 | F016 | complete | 20260529 | 20230428 | 20250331 | 20250430 | 20260331 | 20260430 | 20260529 | 20260529 | 1.0000 |

## Champion History

| fold_id | factor_id | fold_decision_reason |
| --- | --- | --- |
| F001 | PE_TTM | initial_champion |
| F002 | PE_TTM | incumbent_remains_best |
| F003 | PE_TTM | incumbent_remains_best |
| F004 | PE_TTM | incumbent_remains_best |
| F005 | PE_TTM | incumbent_remains_best |
| F006 | PE_TTM | incumbent_remains_best |
| F007 | PE_TTM | incumbent_remains_best |
| F008 | PE_TTM | incumbent_remains_best |
| F009 | PE_TTM | incumbent_remains_best |
| F010 | PE_TTM | incumbent_remains_best |
| F011 | Revenue_Growth | challenger_margin_met |
| F012 | Revenue_Growth | incumbent_remains_best |
| F013 | Revenue_Growth | incumbent_remains_best |
| F014 | Revenue_Growth | incumbent_remains_best |
| F015 | Revenue_Growth | incumbent_remains_best |
| F016 | Revenue_Growth | incumbent_remains_best |

## Pooled Out-of-Sample Metrics

| run_id | scope | fold_id | strategy_id | portfolio_leg | metric_frequency | start_date | end_date | observation_count | annual_return | annual_volatility | sharpe | max_drawdown | win_rate | cumulative_return | average_period_turnover | total_transaction_cost | benchmark_annual_return | excess_annual_return | information_ratio | rank_ic_mean | rank_ic_periods |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| wf_cd46c725e308 | pooled | ALL | Benchmark_000985_CSI | benchmark | daily | 20250304 | 20260701 | 323.0000 | 0.2655 | 0.1880 | 1.3483310272308235 | -0.1375 | 0.5542 | 0.3523 | 0.0000 | 0.0000 | 0.2655 | 0.0000 | <NA> | <NA> | 0.0000 |
| wf_cd46c725e308 | pooled | ALL | Champion_SingleFactor | d1 | daily | 20250304 | 20260701 | 323.0000 | 0.1519 | 0.2444 | 0.7040122779483459 | -0.1991 | 0.5573 | 0.1987 | 0.2916 | 0.0047 | 0.2655 | -0.1136 | -0.7121844191142866 | 0.03416960424699809 | 16.0000 |
| wf_cd46c725e308 | pooled | ALL | Composite_Equal | d1 | daily | 20250304 | 20260701 | 323.0000 | 0.1551 | 0.2313 | 0.7418092278763828 | -0.1962 | 0.5728 | 0.2030 | 0.3460 | 0.0055 | 0.2655 | -0.1104 | -0.7484437670160556 | 0.009068060244855843 | 16.0000 |
| wf_cd46c725e308 | pooled | ALL | Composite_RollingRankIC | d1 | daily | 20250304 | 20260701 | 323.0000 | 0.1619 | 0.2591 | 0.7120606084827452 | -0.2239 | 0.5573 | 0.2121 | 0.2721 | 0.0044 | 0.2655 | -0.1036 | -0.5761841405939939 | 0.05689996472637368 | 16.0000 |
| wf_cd46c725e308 | pooled | ALL | Champion_SingleFactor | d10 | daily | 20250304 | 20260701 | 323.0000 | 0.3754 | 0.2414 | 1.4447182261933669 | -0.2006 | 0.5820 | 0.5046 | 0.2668 | 0.0043 | 0.2655 | 0.1098 | 0.9540347934633879 | 0.03416960424699809 | 16.0000 |
| wf_cd46c725e308 | pooled | ALL | Composite_Equal | d10 | daily | 20250304 | 20260701 | 323.0000 | 0.2845 | 0.2299 | 1.207469391768723 | -0.1838 | 0.5728 | 0.3784 | 0.4348 | 0.0070 | 0.2655 | 0.0190 | 0.26088135227413944 | 0.009068060244855843 | 16.0000 |
| wf_cd46c725e308 | pooled | ALL | Composite_RollingRankIC | d10 | daily | 20250304 | 20260701 | 323.0000 | 0.1797 | 0.1853 | 0.987005805040865 | -0.1574 | 0.5820 | 0.2359 | 0.2723 | 0.0044 | 0.2655 | -0.0859 | -0.6606055262295119 | 0.05689996472637368 | 16.0000 |
| wf_cd46c725e308 | pooled | ALL | Champion_SingleFactor | long_short | monthly | 20250304 | 20260701 | 16.0000 | 0.1742 | 0.0699 | 2.3465927377514677 | -0.0172 | 0.7500 | 0.2387 | 0.5584 | 0.0089 | 0.0000 | 0.1742 | 2.4476929628493655 | 0.03416960424699809 | 16.0000 |
| wf_cd46c725e308 | pooled | ALL | Composite_Equal | long_short | monthly | 20250304 | 20260701 | 16.0000 | 0.0948 | 0.0842 | 1.1211780189458727 | -0.0502 | 0.4375 | 0.1283 | 0.7808 | 0.0125 | 0.0000 | 0.0948 | 1.2271914571306717 | 0.009068060244855843 | 16.0000 |
| wf_cd46c725e308 | pooled | ALL | Composite_RollingRankIC | long_short | monthly | 20250304 | 20260701 | 16.0000 | -0.0158 | 0.1370 | -0.047059922987754035 | -0.1307 | 0.5000 | -0.0211 | 0.5444 | 0.0087 | 0.0000 | -0.0158 | -0.06549821235224938 | 0.05689996472637368 | 16.0000 |
| wf_cd46c725e308 | pooled | ALL | Champion_SingleFactor | top20 | daily | 20250304 | 20260701 | 323.0000 | 0.3172 | 0.2292 | 1.3198197523309148 | -0.1836 | 0.5697 | 0.4235 | 0.2481 | 0.0040 | 0.2655 | 0.0516 | 0.5212925803729441 | 0.03416960424699809 | 16.0000 |
| wf_cd46c725e308 | pooled | ALL | Composite_Equal | top20 | daily | 20250304 | 20260701 | 323.0000 | 0.2721 | 0.2186 | 1.2135003364585595 | -0.1721 | 0.5635 | 0.3614 | 0.3451 | 0.0055 | 0.2655 | 0.0066 | 0.13655625297646726 | 0.009068060244855843 | 16.0000 |
| wf_cd46c725e308 | pooled | ALL | Composite_RollingRankIC | top20 | daily | 20250304 | 20260701 | 323.0000 | 0.1805 | 0.1898 | 0.9719416071954242 | -0.1610 | 0.5820 | 0.2370 | 0.2235 | 0.0036 | 0.2655 | -0.0850 | -0.6723953568375439 | 0.05689996472637368 | 16.0000 |

## Files

- Tables: `tables/fold_schedule.csv`, `selection_log.csv`, `factor_weights.csv`, `oos_daily_returns.csv`, `oos_metrics.csv`
- Images: `images/oos_nav.png`, `fold_comparison.png`, `champion_history.png`
- Export: `exports/walk_forward_champion.zip`
