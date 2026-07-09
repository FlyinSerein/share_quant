# 单因子有效性诊断报告

## 样本与口径

- 数据源：只读连接 `data/share_quant.duckdb`，查询现有研究视图与 silver parquet。
- 输出目录：`research/factor_combo/outputs/diagnostics/`。
- 评价期：`20220101` 至 `20260707`；预热起点 `20210101`。
- 信号与收益：每月最后交易日收盘后打分，下一交易日成交，使用下期持有期个股复权累计收益。
- 缺失收益：停牌缺失按 0 收益计入；非停牌缺失的个股远期收益记为缺失，不参与 IC 和分组收益均值。
- 分组：按截面分数分为 `10` 组，最高分为 D10，多空为 D10 - D1。
- 暴露基准：当期该因子的有效股票池等权分布。
- 基准：中证全指 `000985.CSI`，有效收益日数 `1332`。

## IC 汇总

| factor | rank_ic_mean | rank_ic_annual_icir | ic_mean | ic_annual_icir | average_sample_count |
| --- | --- | --- | --- | --- | --- |
| Volatility | 0.0591 | 0.9406 | 0.0108 | 0.2049 | 4721.4259 |
| Dividend Yield | 0.0436 | 0.8867 | 0.0140 | 0.3399 | 3492.4074 |
| PE TTM | 0.0348 | 0.7024 | 0.0053 | 0.2622 | 3920.5926 |
| Main Net In | 0.0053 | 0.2738 | -0.0008 | -0.0446 | 4769.2222 |
| Revenue Growth | 0.0053 | 0.2009 | 0.0056 | 0.2829 | 4984.5185 |
| Debt/Equity | 0.0012 | 0.0420 | 0.0034 | 0.1461 | 4984.1111 |

## 中性化 IC 汇总

| factor | rank_ic_mean | rank_ic_annual_icir | ic_mean | ic_annual_icir | average_sample_count |
| --- | --- | --- | --- | --- | --- |
| Volatility | 0.0743 | 1.8080 | 0.0282 | 0.7686 | 4721.4259 |
| Dividend Yield | 0.0443 | 1.9610 | 0.0209 | 0.9105 | 3492.4074 |
| PE TTM | 0.0356 | 2.1193 | 0.0087 | 0.6749 | 3920.5926 |
| ROE | 0.0120 | 0.4799 | 0.0053 | 0.2657 | 4984.4074 |
| Revenue Growth | 0.0086 | 0.5397 | 0.0074 | 0.5687 | 4984.5185 |
| Main Net In | 0.0084 | 0.5984 | 0.0039 | 0.3577 | 4769.2222 |

## 多空收益

| factor | annual_return | annual_volatility | max_drawdown | sharpe | win_rate |
| --- | --- | --- | --- | --- | --- |
| Revenue Growth | 0.0479 | 0.1171 | -0.2313 | 0.4086 | 0.4630 |
| Debt/Equity | 0.0277 | 0.1393 | -0.1208 | 0.1987 | 0.5926 |
| Dividend Yield | -0.0192 | 0.2295 | -0.4925 | -0.0838 | 0.4815 |
| Holder Concentration | -0.0225 | 0.0588 | -0.1047 | -0.3822 | 0.3889 |
| Main Net In | -0.0342 | 0.0990 | -0.2279 | -0.3452 | 0.4815 |
| PE TTM | -0.0436 | 0.2054 | -0.4017 | -0.2120 | 0.5000 |

## 分组单调性

| factor | monotonic_up_period_share | average_spearman_decile_ic |
| --- | --- | --- |
| Main Net In | 0.0000 | 0.0844 |
| Revenue Growth | 0.0185 | 0.0438 |
| Turnover 20D | 0.0000 | 0.0406 |
| Dividend Yield | 0.0370 | 0.0117 |
| Debt/Equity | 0.0000 | 0.0114 |
| Momentum 60D | 0.0185 | 0.0097 |

## 市值暴露

| factor | average_active_log_total_mv | average_portfolio_size_percentile |
| --- | --- | --- |
| Debt/Equity | -0.2935 | 0.4247 |
| Turnover 20D | -0.2826 | 0.4347 |
| Holder Concentration | 0.0343 | 0.5027 |
| Revenue Growth | 0.1450 | 0.5408 |
| Momentum 60D | 0.1723 | 0.5460 |
| Gross Margin | 0.1903 | 0.5533 |
| Dividend Yield | 0.3395 | 0.5745 |
| Main Net In | 0.4419 | 0.6197 |
| PE TTM | 0.5120 | 0.6249 |
| Volatility | 0.5490 | 0.6254 |
| ROE | 0.6226 | 0.6548 |

## 行业暴露

| factor | average_abs_active_weight | max_abs_active_weight | unknown_active_weight |
| --- | --- | --- | --- |
| Gross Margin | 0.0224 | 0.2110 | -0.0080 |
| Volatility | 0.0185 | 0.0825 | -0.0069 |
| PE TTM | 0.0179 | 0.0907 | -0.0015 |
| Dividend Yield | 0.0165 | 0.0825 | -0.0046 |
| Momentum 60D | 0.0129 | 0.2352 | 0.0009 |
| Debt/Equity | 0.0127 | 0.0894 | -0.0012 |
| Turnover 20D | 0.0124 | 0.1616 | -0.0004 |
| Revenue Growth | 0.0112 | 0.1016 | 0.0066 |
| ROE | 0.0101 | 0.0653 | 0.0011 |
| Main Net In | 0.0084 | 0.0870 | -0.0035 |
| Holder Concentration | 0.0070 | 0.0413 | 0.0005 |

## 输出文件

- IC：`ic_by_period.csv`、`ic_summary.csv`、`neutralized_ic_by_period.csv`、`neutralized_ic_summary.csv`
- 分组与多空：`decile_returns.csv`、`decile_summary.csv`、`long_short_returns.csv`、`long_short_metrics.csv`
- 年度：`yearly_factor_returns.csv`、`yearly_long_short_returns.csv`
- 暴露：`size_exposure.csv`、`industry_exposure.csv`

## 图像

- `images/rank_ic_comparison.png`
- `images/decile_returns.png`
- `images/long_short_annual_return.png`
- `images/size_exposure.png`
- `images/industry_exposure.png`
