from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Iterable

import duckdb
import numpy as np
import pandas as pd

try:
    from .factor_backtest import (
        DATE_FMT,
        FACTOR_DISPLAY_NAMES,
        FactorResearchRunner,
        backtest_monthly_top_quantile,
        build_suspension_matrix,
        build_rebalance_calendar,
        filter_scores_for_execution_universe,
        neutralize_scores,
        normalize_factor_panel,
        select_top_quantile_weights,
        yyyymmdd,
    )
except ImportError:  # pragma: no cover - supports direct script execution.
    from factor_backtest import (
        DATE_FMT,
        FACTOR_DISPLAY_NAMES,
        FactorResearchRunner,
        backtest_monthly_top_quantile,
        build_suspension_matrix,
        build_rebalance_calendar,
        filter_scores_for_execution_universe,
        neutralize_scores,
        normalize_factor_panel,
        select_top_quantile_weights,
        yyyymmdd,
    )


def build_forward_period_returns(
    returns: pd.DataFrame,
    rebalance_calendar: pd.DataFrame,
    end_date: str,
    ts_codes: Iterable[str] | None = None,
    suspensions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    required_returns = {"trade_date", "ts_code", "return_adjusted"}
    required_calendar = {"signal_date", "exec_date"}
    if required_returns - set(returns.columns):
        raise ValueError("returns must include trade_date, ts_code, return_adjusted")
    if required_calendar - set(rebalance_calendar.columns):
        raise ValueError("rebalance_calendar must include signal_date, exec_date")
    if returns.empty or rebalance_calendar.empty:
        return pd.DataFrame(
            columns=[
                "signal_date",
                "exec_date",
                "ts_code",
                "forward_return",
                "holding_days",
                "missing_return_count",
                "suspended_return_count",
                "invalid_missing_return_count",
            ]
        )

    return_matrix = (
        returns.assign(return_adjusted=pd.to_numeric(returns["return_adjusted"], errors="coerce"))
        .pivot(index="trade_date", columns="ts_code", values="return_adjusted")
        .sort_index()
    )
    columns = sorted(set(str(code) for code in ts_codes)) if ts_codes is not None else return_matrix.columns.to_list()
    trade_dates = return_matrix.index.astype(str).to_list()
    suspension_matrix = build_suspension_matrix(suspensions, trade_dates, columns)
    periods = rebalance_calendar.sort_values("signal_date").reset_index(drop=True)
    rows: list[pd.DataFrame] = []

    for idx, period in periods.iterrows():
        signal_date = str(period["signal_date"])
        exec_date = str(period["exec_date"])
        next_exec = str(periods.loc[idx + 1, "exec_date"]) if idx + 1 < len(periods) else None
        period_dates = [date for date in trade_dates if date > exec_date and date <= (next_exec or end_date)]
        if not period_dates:
            continue
        available = return_matrix.reindex(index=period_dates, columns=columns)
        suspended = suspension_matrix.reindex(index=period_dates, columns=columns, fill_value=False).astype(bool)
        valid_returns = available.notna()
        invalid_missing = ~(valid_returns | suspended)
        suspended_counts = (available.isna() & suspended).sum(axis=0)
        invalid_missing_counts = invalid_missing.sum(axis=0)
        forward_returns = (1.0 + available.fillna(0.0)).prod(axis=0) - 1.0
        forward_returns = forward_returns.mask(invalid_missing_counts > 0)
        frame = pd.DataFrame(
            {
                "signal_date": signal_date,
                "exec_date": exec_date,
                "ts_code": columns,
                "forward_return": forward_returns.to_numpy(),
                "holding_days": len(period_dates),
                "missing_return_count": available.isna().sum(axis=0).to_numpy(),
                "suspended_return_count": suspended_counts.to_numpy(),
                "invalid_missing_return_count": invalid_missing_counts.to_numpy(),
            }
        )
        rows.append(frame)

    if not rows:
        return pd.DataFrame(
            columns=[
                "signal_date",
                "exec_date",
                "ts_code",
                "forward_return",
                "holding_days",
                "missing_return_count",
                "suspended_return_count",
                "invalid_missing_return_count",
            ]
        )
    return pd.concat(rows, ignore_index=True)


def compute_ic_by_period(scores: pd.DataFrame, forward_returns: pd.DataFrame, score_col: str = "score") -> pd.DataFrame:
    required_scores = {"factor", "trade_date", "ts_code", score_col}
    required_returns = {"signal_date", "ts_code", "forward_return"}
    if required_scores - set(scores.columns):
        raise ValueError(f"scores must include factor, trade_date, ts_code, {score_col}")
    if required_returns - set(forward_returns.columns):
        raise ValueError("forward_returns must include signal_date, ts_code, forward_return")

    if scores.empty or forward_returns.empty:
        return pd.DataFrame(columns=["factor", "trade_date", "ic", "rank_ic", "sample_count"])

    merged = scores.merge(
        forward_returns.rename(columns={"signal_date": "trade_date"}),
        on=["trade_date", "ts_code"],
        how="left",
    )
    rows = []
    for (factor, trade_date), group in merged.groupby(["factor", "trade_date"], sort=True):
        valid = group[[score_col, "forward_return"]].apply(pd.to_numeric, errors="coerce").dropna()
        ic = pd.NA
        rank_ic = pd.NA
        if len(valid) >= 3 and valid[score_col].nunique() > 1 and valid["forward_return"].nunique() > 1:
            ic = float(valid[score_col].corr(valid["forward_return"]))
            rank_ic = float(valid[score_col].rank(method="average").corr(valid["forward_return"].rank(method="average")))
        rows.append(
            {
                "factor": factor,
                "trade_date": trade_date,
                "ic": ic,
                "rank_ic": rank_ic,
                "sample_count": int(len(valid)),
            }
        )
    return pd.DataFrame(rows)


def summarize_ic(ic_by_period: pd.DataFrame) -> pd.DataFrame:
    if ic_by_period.empty:
        return pd.DataFrame()
    rows = []
    for factor, group in ic_by_period.groupby("factor", sort=True):
        ic = pd.to_numeric(group["ic"], errors="coerce").dropna()
        rank_ic = pd.to_numeric(group["rank_ic"], errors="coerce").dropna()
        rows.append(
            {
                "factor": factor,
                "period_count": int(group["trade_date"].nunique()),
                "ic_mean": _mean_or_na(ic),
                "ic_std": _std_or_na(ic),
                "ic_win_rate": _mean_or_na(ic > 0),
                "ic_monthly_icir": _icir(ic, 1.0),
                "ic_annual_icir": _icir(ic, math.sqrt(12.0)),
                "rank_ic_mean": _mean_or_na(rank_ic),
                "rank_ic_std": _std_or_na(rank_ic),
                "rank_ic_win_rate": _mean_or_na(rank_ic > 0),
                "rank_ic_monthly_icir": _icir(rank_ic, 1.0),
                "rank_ic_annual_icir": _icir(rank_ic, math.sqrt(12.0)),
                "average_sample_count": float(group["sample_count"].mean()),
            }
        )
    return pd.DataFrame(rows).sort_values("rank_ic_mean", ascending=False, na_position="last").reset_index(drop=True)


def assign_score_deciles(scores: pd.DataFrame, bucket_count: int = 10, score_col: str = "score") -> pd.DataFrame:
    required = {"factor", "trade_date", "ts_code", score_col}
    if required - set(scores.columns):
        raise ValueError(f"scores must include factor, trade_date, ts_code, {score_col}")
    if bucket_count < 2:
        raise ValueError("bucket_count must be at least 2")
    valid = scores.dropna(subset=[score_col]).copy()
    if valid.empty:
        return pd.DataFrame(columns=["factor", "trade_date", "ts_code", "decile"])

    frames = []
    for (_factor, _trade_date), group in valid.groupby(["factor", "trade_date"], sort=True):
        ranked = group.sort_values([score_col, "ts_code"], ascending=[True, True]).copy()
        n = len(ranked)
        ranked["decile"] = np.ceil(np.arange(1, n + 1) * bucket_count / n).astype(int)
        ranked["decile"] = ranked["decile"].clip(1, bucket_count)
        frames.append(ranked[["factor", "trade_date", "ts_code", "decile"]])
    return pd.concat(frames, ignore_index=True)


def compute_decile_returns(deciles: pd.DataFrame, forward_returns: pd.DataFrame) -> pd.DataFrame:
    if deciles.empty or forward_returns.empty:
        return pd.DataFrame(columns=["factor", "trade_date", "decile", "average_forward_return", "stock_count"])
    merged = deciles.merge(
        forward_returns.rename(columns={"signal_date": "trade_date"}),
        on=["trade_date", "ts_code"],
        how="left",
    )
    merged["forward_return"] = pd.to_numeric(merged["forward_return"], errors="coerce")
    return (
        merged.groupby(["factor", "trade_date", "decile"], as_index=False)
        .agg(average_forward_return=("forward_return", "mean"), stock_count=("forward_return", "count"))
        .sort_values(["factor", "trade_date", "decile"])
        .reset_index(drop=True)
    )


def summarize_decile_returns(decile_returns: pd.DataFrame, bucket_count: int = 10) -> pd.DataFrame:
    if decile_returns.empty:
        return pd.DataFrame()
    rows = []
    for factor, group in decile_returns.groupby("factor", sort=True):
        pivot = group.pivot(index="trade_date", columns="decile", values="average_forward_return").sort_index()
        means = pivot.mean(axis=0)
        row: dict[str, object] = {
            "factor": factor,
            "period_count": int(pivot.shape[0]),
            "monotonic_up_period_share": _mean_or_na(pivot.apply(lambda r: r.dropna().is_monotonic_increasing if r.dropna().shape[0] >= 2 else pd.NA, axis=1).dropna()),
            "average_spearman_decile_ic": _mean_or_na(
                pivot.apply(lambda r: pd.Series(r.index, index=r.index).corr(r, method="spearman") if r.dropna().shape[0] >= 3 else pd.NA, axis=1).dropna()
            ),
        }
        for decile in range(1, bucket_count + 1):
            series = pivot[decile].dropna() if decile in pivot.columns else pd.Series(dtype="float64")
            row[f"d{decile}_mean_return"] = _mean_or_na(series)
            row[f"d{decile}_annual_return"] = _periodic_annual_return(series)
        rows.append(row)
    return pd.DataFrame(rows).sort_values("average_spearman_decile_ic", ascending=False, na_position="last").reset_index(drop=True)


def compute_long_short_returns(decile_returns: pd.DataFrame, bucket_count: int = 10) -> pd.DataFrame:
    if decile_returns.empty:
        return pd.DataFrame(columns=["factor", "trade_date", "long_short_return"])
    pivot = decile_returns.pivot(index=["factor", "trade_date"], columns="decile", values="average_forward_return")
    if 1 not in pivot.columns or bucket_count not in pivot.columns:
        return pd.DataFrame(columns=["factor", "trade_date", "long_short_return"])
    result = (pivot[bucket_count] - pivot[1]).reset_index(name="long_short_return")
    return result.sort_values(["factor", "trade_date"]).reset_index(drop=True)


def compute_periodic_return_metrics(period_returns: pd.DataFrame, return_col: str, factor_col: str = "factor") -> pd.DataFrame:
    if period_returns.empty:
        return pd.DataFrame()
    rows = []
    for factor, group in period_returns.groupby(factor_col, sort=True):
        series = pd.to_numeric(group[return_col], errors="coerce").dropna()
        nav = (1.0 + series).cumprod()
        drawdown = nav / nav.cummax() - 1.0 if not nav.empty else pd.Series(dtype="float64")
        rows.append(
            {
                factor_col: factor,
                "period_count": int(series.shape[0]),
                "annual_return": _periodic_annual_return(series),
                "annual_volatility": _std_or_na(series) * math.sqrt(12.0) if len(series) else pd.NA,
                "sharpe": _sharpe(series),
                "max_drawdown": drawdown.min() if not drawdown.empty else pd.NA,
                "win_rate": _mean_or_na(series > 0),
                "cumulative_return": nav.iloc[-1] - 1.0 if not nav.empty else pd.NA,
            }
        )
    return pd.DataFrame(rows).sort_values("annual_return", ascending=False, na_position="last").reset_index(drop=True)


def compute_yearly_returns(daily_returns: pd.DataFrame, return_col: str = "portfolio_return") -> pd.DataFrame:
    if daily_returns.empty:
        return pd.DataFrame(columns=["factor", "year", "year_return", "trading_days"])
    frame = daily_returns.copy()
    frame["year"] = pd.to_datetime(frame["trade_date"], format=DATE_FMT).dt.year
    return (
        frame.groupby(["factor", "year"], as_index=False)
        .agg(year_return=(return_col, lambda s: (1.0 + pd.to_numeric(s, errors="coerce").fillna(0.0)).prod() - 1.0), trading_days=(return_col, "count"))
        .sort_values(["factor", "year"])
        .reset_index(drop=True)
    )


def compute_yearly_period_returns(period_returns: pd.DataFrame, return_col: str) -> pd.DataFrame:
    if period_returns.empty:
        return pd.DataFrame(columns=["factor", "year", "year_return", "period_count"])
    frame = period_returns.copy()
    frame["year"] = pd.to_datetime(frame["trade_date"], format=DATE_FMT).dt.year
    return (
        frame.groupby(["factor", "year"], as_index=False)
        .agg(year_return=(return_col, lambda s: (1.0 + pd.to_numeric(s, errors="coerce").fillna(0.0)).prod() - 1.0), period_count=(return_col, "count"))
        .sort_values(["factor", "year"])
        .reset_index(drop=True)
    )


def compute_size_exposure(scores: pd.DataFrame, weights: pd.DataFrame, exposures: pd.DataFrame) -> pd.DataFrame:
    if scores.empty or weights.empty:
        return pd.DataFrame()
    valid_scores = scores.dropna(subset=["score"]).merge(exposures, on=["trade_date", "ts_code"], how="left")
    valid_scores["log_total_mv"] = pd.to_numeric(valid_scores["log_total_mv"], errors="coerce")
    weights_exp = weights.merge(exposures, on=["trade_date", "ts_code"], how="left")
    weights_exp["log_total_mv"] = pd.to_numeric(weights_exp["log_total_mv"], errors="coerce")
    percentile = valid_scores.copy()
    percentile["size_percentile"] = percentile.groupby(["factor", "trade_date"])["log_total_mv"].rank(pct=True)
    weights_exp = weights_exp.merge(percentile[["factor", "trade_date", "ts_code", "size_percentile"]], on=["factor", "trade_date", "ts_code"], how="left")

    rows = []
    for (factor, trade_date), base in valid_scores.groupby(["factor", "trade_date"], sort=True):
        port = weights_exp[(weights_exp["factor"] == factor) & (weights_exp["trade_date"] == trade_date)]
        baseline_log_mv = base["log_total_mv"].mean()
        portfolio_log_mv = _weighted_mean(port["log_total_mv"], port["weight"])
        portfolio_percentile = _weighted_mean(port["size_percentile"], port["weight"])
        rows.append(
            {
                "factor": factor,
                "trade_date": trade_date,
                "baseline_log_total_mv": baseline_log_mv,
                "portfolio_log_total_mv": portfolio_log_mv,
                "active_log_total_mv": portfolio_log_mv - baseline_log_mv if pd.notna(portfolio_log_mv) and pd.notna(baseline_log_mv) else pd.NA,
                "portfolio_size_percentile": portfolio_percentile,
                "baseline_count": int(base.shape[0]),
                "holding_count": int(port.shape[0]),
                "missing_size_count": int(base["log_total_mv"].isna().sum()),
            }
        )
    return pd.DataFrame(rows)


def compute_industry_exposure(scores: pd.DataFrame, weights: pd.DataFrame, exposures: pd.DataFrame) -> pd.DataFrame:
    if scores.empty or weights.empty:
        return pd.DataFrame()
    valid_scores = scores.dropna(subset=["score"]).merge(exposures[["trade_date", "ts_code", "industry"]], on=["trade_date", "ts_code"], how="left")
    valid_scores["industry"] = valid_scores["industry"].fillna("Unknown")
    weights_exp = weights.merge(exposures[["trade_date", "ts_code", "industry"]], on=["trade_date", "ts_code"], how="left")
    weights_exp["industry"] = weights_exp["industry"].fillna("Unknown")
    rows = []
    for (factor, trade_date), base in valid_scores.groupby(["factor", "trade_date"], sort=True):
        base_weights = base["industry"].value_counts(normalize=True)
        port = weights_exp[(weights_exp["factor"] == factor) & (weights_exp["trade_date"] == trade_date)]
        port_weights = port.groupby("industry")["weight"].sum()
        for industry in sorted(set(base_weights.index).union(set(port_weights.index))):
            baseline = float(base_weights.get(industry, 0.0))
            portfolio = float(port_weights.get(industry, 0.0))
            rows.append(
                {
                    "factor": factor,
                    "trade_date": trade_date,
                    "industry": industry,
                    "baseline_weight": baseline,
                    "portfolio_weight": portfolio,
                    "active_weight": portfolio - baseline,
                    "baseline_count": int((base["industry"] == industry).sum()),
                    "holding_count": int((port["industry"] == industry).sum()),
                }
            )
    return pd.DataFrame(rows)


class FactorDiagnosticsRunner:
    def __init__(
        self,
        project_root: Path,
        db_path: Path,
        output_dir: Path,
        start: str = "2022-01-01",
        end: str | None = None,
        warmup_start: str = "2021-01-01",
        benchmark: str = "000985.CSI",
        bucket_count: int = 10,
        transaction_cost: float = 0.001,
    ) -> None:
        self.project_root = Path(project_root)
        self.db_path = Path(db_path)
        self.output_dir = Path(output_dir)
        self.start = yyyymmdd(start)
        self.end = yyyymmdd(end) if end else None
        self.warmup_start = yyyymmdd(warmup_start)
        self.benchmark = benchmark
        self.bucket_count = bucket_count
        self.transaction_cost = transaction_cost
        self._research = FactorResearchRunner(
            project_root=project_root,
            db_path=db_path,
            output_dir=output_dir,
            start=start,
            end=end,
            warmup_start=warmup_start,
            benchmark=benchmark,
            transaction_cost=transaction_cost,
        )

    def run(self) -> dict[str, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(self.db_path), read_only=True) as con:
            end_date = self.end or self._research._latest_trade_date(con)
            all_trade_dates = self._research._load_trade_dates(con, self.warmup_start, end_date)
            rebalance_calendar = build_rebalance_calendar(all_trade_dates, self.start, end_date)
            signal_dates = rebalance_calendar["signal_date"].drop_duplicates().reset_index(drop=True)
            con.register("signal_dates", pd.DataFrame({"trade_date": signal_dates}))
            raw_factors = self._research._load_factor_panel(con, signal_dates, end_date)
            scores = normalize_factor_panel(raw_factors)
            execution_universe = self._research._load_execution_universe(con, rebalance_calendar)
            tradable_scores = filter_scores_for_execution_universe(scores, rebalance_calendar, execution_universe)
            weights = select_top_quantile_weights(tradable_scores, quantile=0.2)
            returns = self._research._load_returns(con, self.start, end_date)
            suspensions = self._research._load_suspensions(con, self.start, end_date)
            benchmark_returns = self._research._load_benchmark(con, self.warmup_start, end_date)
            exposures = self._load_exposures(con, signal_dates)

        forward_returns = build_forward_period_returns(
            returns,
            rebalance_calendar,
            end_date,
            ts_codes=scores["ts_code"].dropna().unique(),
            suspensions=suspensions,
        )
        ic_by_period = compute_ic_by_period(scores, forward_returns)
        ic_summary = summarize_ic(ic_by_period)
        deciles = assign_score_deciles(scores, self.bucket_count)
        decile_returns = compute_decile_returns(deciles, forward_returns)
        decile_summary = summarize_decile_returns(decile_returns, self.bucket_count)
        long_short_returns = compute_long_short_returns(decile_returns, self.bucket_count)
        long_short_metrics = compute_periodic_return_metrics(long_short_returns, "long_short_return")
        portfolio_returns, _turnover = backtest_monthly_top_quantile(
            weights,
            returns,
            rebalance_calendar,
            end_date=end_date,
            transaction_cost=self.transaction_cost,
            suspensions=suspensions,
        )
        yearly_factor_returns = compute_yearly_returns(portfolio_returns)
        yearly_long_short_returns = compute_yearly_period_returns(long_short_returns, "long_short_return")
        neutralized_scores = neutralize_scores(scores, exposures)
        neutralized_ic_by_period = compute_ic_by_period(neutralized_scores, forward_returns, score_col="neutralized_score")
        neutralized_ic_summary = summarize_ic(neutralized_ic_by_period)
        size_exposure = compute_size_exposure(scores, weights, exposures)
        industry_exposure = compute_industry_exposure(scores, weights, exposures)

        return self._write_outputs(
            ic_by_period=ic_by_period,
            ic_summary=ic_summary,
            neutralized_ic_by_period=neutralized_ic_by_period,
            neutralized_ic_summary=neutralized_ic_summary,
            decile_returns=decile_returns,
            decile_summary=decile_summary,
            long_short_returns=long_short_returns,
            long_short_metrics=long_short_metrics,
            yearly_factor_returns=yearly_factor_returns,
            yearly_long_short_returns=yearly_long_short_returns,
            size_exposure=size_exposure,
            industry_exposure=industry_exposure,
            end_date=end_date,
            benchmark_returns=benchmark_returns,
        )

    def _load_exposures(self, con: duckdb.DuckDBPyConnection, signal_dates: pd.Series) -> pd.DataFrame:
        return self._research._load_exposures(con, signal_dates)

    def _write_outputs(self, **frames: object) -> dict[str, Path]:
        mapping = {
            "ic_by_period": "ic_by_period.csv",
            "ic_summary": "ic_summary.csv",
            "neutralized_ic_by_period": "neutralized_ic_by_period.csv",
            "neutralized_ic_summary": "neutralized_ic_summary.csv",
            "decile_returns": "decile_returns.csv",
            "decile_summary": "decile_summary.csv",
            "long_short_returns": "long_short_returns.csv",
            "long_short_metrics": "long_short_metrics.csv",
            "yearly_factor_returns": "yearly_factor_returns.csv",
            "yearly_long_short_returns": "yearly_long_short_returns.csv",
            "size_exposure": "size_exposure.csv",
            "industry_exposure": "industry_exposure.csv",
        }
        paths: dict[str, Path] = {}
        for key, filename in mapping.items():
            frame = frames[key]
            path = self.output_dir / filename
            assert isinstance(frame, pd.DataFrame)
            frame.to_csv(path, index=False, encoding="utf-8-sig")
            paths[key] = path
        image_paths = self._write_images(
            ic_summary=frames["ic_summary"],
            neutralized_ic_summary=frames["neutralized_ic_summary"],
            decile_returns=frames["decile_returns"],
            long_short_metrics=frames["long_short_metrics"],
            size_exposure=frames["size_exposure"],
            industry_exposure=frames["industry_exposure"],
        )
        paths.update(image_paths)
        report_path = self.output_dir / "diagnostics_report.md"
        self._write_report(
            report_path,
            ic_summary=frames["ic_summary"],
            neutralized_ic_summary=frames["neutralized_ic_summary"],
            decile_summary=frames["decile_summary"],
            long_short_metrics=frames["long_short_metrics"],
            size_exposure=frames["size_exposure"],
            industry_exposure=frames["industry_exposure"],
            end_date=frames["end_date"],
            benchmark_returns=frames["benchmark_returns"],
            image_paths=image_paths,
        )
        paths["report"] = report_path
        return paths

    def _write_report(
        self,
        path: Path,
        ic_summary: pd.DataFrame,
        neutralized_ic_summary: pd.DataFrame,
        decile_summary: pd.DataFrame,
        long_short_metrics: pd.DataFrame,
        size_exposure: pd.DataFrame,
        industry_exposure: pd.DataFrame,
        end_date: str,
        benchmark_returns: pd.DataFrame,
        image_paths: dict[str, Path],
    ) -> None:
        top_rank_ic = ic_summary.head(6) if not ic_summary.empty else pd.DataFrame()
        top_long_short = long_short_metrics.head(6) if not long_short_metrics.empty else pd.DataFrame()
        size_summary = _summarize_size_exposure_for_report(size_exposure)
        industry_summary = _summarize_industry_exposure_for_report(industry_exposure)
        benchmark_count = int(benchmark_returns["benchmark_return"].notna().sum()) if not benchmark_returns.empty else 0
        image_lines = "\n".join(f"- `{path.relative_to(self.output_dir).as_posix()}`" for path in image_paths.values())
        content = f"""# 单因子有效性诊断报告

## 样本与口径

- 数据源：只读连接 `data/share_quant.duckdb`，查询现有研究视图与 silver parquet。
- 输出目录：`research/factor_combo/outputs/diagnostics/`。
- 评价期：`{self.start}` 至 `{end_date}`；预热起点 `{self.warmup_start}`。
- 信号与收益：每月最后交易日收盘后打分，下一交易日成交，使用下期持有期个股复权累计收益。
- 缺失收益：停牌缺失按 0 收益计入；非停牌缺失的个股远期收益记为缺失，不参与 IC 和分组收益均值。
- 分组：按截面分数分为 `{self.bucket_count}` 组，最高分为 D{self.bucket_count}，多空为 D{self.bucket_count} - D1。
- 暴露基准：当期该因子的有效股票池等权分布。
- 基准：中证全指 `{self.benchmark}`，有效收益日数 `{benchmark_count}`。

## IC 汇总

{_markdown_table(_format_report_table(top_rank_ic, ["factor", "rank_ic_mean", "rank_ic_annual_icir", "ic_mean", "ic_annual_icir", "average_sample_count"]))}

## 中性化 IC 汇总

{_markdown_table(_format_report_table(neutralized_ic_summary.head(6), ["factor", "rank_ic_mean", "rank_ic_annual_icir", "ic_mean", "ic_annual_icir", "average_sample_count"]))}

## 多空收益

{_markdown_table(_format_report_table(top_long_short, ["factor", "annual_return", "annual_volatility", "max_drawdown", "sharpe", "win_rate"]))}

## 分组单调性

{_markdown_table(_format_report_table(decile_summary.head(6), ["factor", "monotonic_up_period_share", "average_spearman_decile_ic"]))}

## 市值暴露

{_markdown_table(_format_report_table(size_summary, ["factor", "average_active_log_total_mv", "average_portfolio_size_percentile"]))}

## 行业暴露

{_markdown_table(_format_report_table(industry_summary, ["factor", "average_abs_active_weight", "max_abs_active_weight", "unknown_active_weight"]))}

## 输出文件

- IC：`ic_by_period.csv`、`ic_summary.csv`、`neutralized_ic_by_period.csv`、`neutralized_ic_summary.csv`
- 分组与多空：`decile_returns.csv`、`decile_summary.csv`、`long_short_returns.csv`、`long_short_metrics.csv`
- 年度：`yearly_factor_returns.csv`、`yearly_long_short_returns.csv`
- 暴露：`size_exposure.csv`、`industry_exposure.csv`

## 图像

{image_lines}
"""
        path.write_text(content, encoding="utf-8")

    def _write_images(
        self,
        ic_summary: pd.DataFrame,
        neutralized_ic_summary: pd.DataFrame,
        decile_returns: pd.DataFrame,
        long_short_metrics: pd.DataFrame,
        size_exposure: pd.DataFrame,
        industry_exposure: pd.DataFrame,
    ) -> dict[str, Path]:
        try:
            import matplotlib.pyplot as plt
            from matplotlib.ticker import PercentFormatter
        except ImportError:
            return {}

        images = self.output_dir / "images"
        images.mkdir(parents=True, exist_ok=True)
        plt.style.use("seaborn-v0_8-whitegrid")
        paths: dict[str, Path] = {}

        if not ic_summary.empty and not neutralized_ic_summary.empty:
            raw = ic_summary[["factor", "rank_ic_mean"]].rename(columns={"rank_ic_mean": "raw_rank_ic"})
            neutralized = neutralized_ic_summary[["factor", "rank_ic_mean"]].rename(columns={"rank_ic_mean": "neutralized_rank_ic"})
            table = raw.merge(neutralized, on="factor", how="outer").sort_values("raw_rank_ic", ascending=True)
            labels = [_display_factor(value) for value in table["factor"]]
            y = np.arange(len(table))
            height = max(4.5, len(table) * 0.42)
            path = images / "rank_ic_comparison.png"
            fig, ax = plt.subplots(figsize=(10, height))
            ax.barh(y - 0.18, table["raw_rank_ic"], height=0.36, label="Raw RankIC", color="#4c78a8")
            ax.barh(y + 0.18, table["neutralized_rank_ic"], height=0.36, label="Neutralized RankIC", color="#f58518")
            ax.axvline(0, color="#333333", linewidth=0.8)
            ax.set_yticks(y, labels)
            ax.set_xlabel("Mean RankIC")
            ax.set_title("Raw vs Neutralized RankIC")
            ax.legend(frameon=False)
            fig.tight_layout()
            fig.savefig(path, dpi=170)
            plt.close(fig)
            paths["rank_ic_image"] = path

        if not decile_returns.empty:
            table = (
                decile_returns.groupby(["factor", "decile"], as_index=False)["average_forward_return"]
                .mean()
                .sort_values(["factor", "decile"])
            )
            path = images / "decile_returns.png"
            fig, ax = plt.subplots(figsize=(12, 7))
            for factor, group in table.groupby("factor"):
                ax.plot(group["decile"], group["average_forward_return"], marker="o", linewidth=1.3, alpha=0.85, label=_display_factor(factor))
            ax.axhline(0, color="#333333", linewidth=0.8)
            ax.set_title("Average Forward Return by Decile")
            ax.set_xlabel("Score Decile (D10 = Highest Score)")
            ax.set_ylabel("Average Holding-Period Return")
            ax.yaxis.set_major_formatter(PercentFormatter(1.0))
            ax.legend(fontsize=8, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.12), frameon=False)
            fig.tight_layout(rect=(0, 0.08, 1, 1))
            fig.savefig(path, dpi=170)
            plt.close(fig)
            paths["decile_returns_image"] = path

        if not long_short_metrics.empty:
            table = long_short_metrics.sort_values("annual_return", ascending=True)
            path = images / "long_short_annual_return.png"
            fig, ax = plt.subplots(figsize=(10, max(4.5, len(table) * 0.4)))
            colors = ["#54a24b" if value >= 0 else "#e45756" for value in table["annual_return"]]
            ax.barh([_display_factor(value) for value in table["factor"]], table["annual_return"], color=colors)
            ax.axvline(0, color="#333333", linewidth=0.8)
            ax.set_title("Top-Bottom Long/Short Annual Return")
            ax.set_xlabel("Annual Return")
            ax.xaxis.set_major_formatter(PercentFormatter(1.0))
            fig.tight_layout()
            fig.savefig(path, dpi=170)
            plt.close(fig)
            paths["long_short_image"] = path

        size_summary = _summarize_size_exposure_for_report(size_exposure)
        if not size_summary.empty:
            table = size_summary.sort_values("average_active_log_total_mv", ascending=True)
            path = images / "size_exposure.png"
            fig, ax = plt.subplots(figsize=(10, max(4.5, len(table) * 0.4)))
            colors = ["#e45756" if value < 0 else "#4c78a8" for value in table["average_active_log_total_mv"]]
            ax.barh([_display_factor(value) for value in table["factor"]], table["average_active_log_total_mv"], color=colors)
            ax.axvline(0, color="#333333", linewidth=0.8)
            ax.set_title("Top20% Active Size Exposure")
            ax.set_xlabel("Portfolio log(total_mv) - Universe log(total_mv)")
            fig.tight_layout()
            fig.savefig(path, dpi=170)
            plt.close(fig)
            paths["size_exposure_image"] = path

        industry_summary = _summarize_industry_exposure_for_report(industry_exposure)
        if not industry_summary.empty:
            table = industry_summary.sort_values("average_abs_active_weight", ascending=True)
            path = images / "industry_exposure.png"
            fig, ax = plt.subplots(figsize=(10, max(4.5, len(table) * 0.4)))
            ax.barh([_display_factor(value) for value in table["factor"]], table["average_abs_active_weight"], color="#72b7b2")
            ax.set_title("Top20% Average Absolute Industry Active Weight")
            ax.set_xlabel("Average Absolute Active Weight")
            ax.xaxis.set_major_formatter(PercentFormatter(1.0))
            fig.tight_layout()
            fig.savefig(path, dpi=170)
            plt.close(fig)
            paths["industry_exposure_image"] = path

        return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run single-factor validity diagnostics.")
    parser.add_argument("--start", default="2022-01-01", help="Evaluation start date, YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="Evaluation end date, YYYY-MM-DD. Defaults to latest local trade date.")
    parser.add_argument("--warmup-start", default="2021-01-01", help="Warmup start date for rolling factors.")
    parser.add_argument("--benchmark", default="000985.CSI", help="Benchmark index code.")
    parser.add_argument("--bucket-count", type=int, default=10, help="Number of score buckets for grouped returns.")
    parser.add_argument("--transaction-cost", type=float, default=0.001, help="One-way transaction cost for yearly Top20%% returns.")
    parser.add_argument("--output-dir", default=None, help="Output directory. Defaults to outputs/diagnostics/.")
    return parser


def run_from_args(argv: list[str] | None = None) -> dict[str, Path]:
    args = build_parser().parse_args(argv)
    here = Path(__file__).resolve().parent
    project_root = here.parents[1]
    output_dir = Path(args.output_dir).resolve() if args.output_dir else here / "outputs" / "diagnostics"
    runner = FactorDiagnosticsRunner(
        project_root=project_root,
        db_path=project_root / "data" / "share_quant.duckdb",
        output_dir=output_dir,
        start=args.start,
        end=args.end,
        warmup_start=args.warmup_start,
        benchmark=args.benchmark,
        bucket_count=args.bucket_count,
        transaction_cost=args.transaction_cost,
    )
    return runner.run()


def _weighted_mean(values: pd.Series, weights: pd.Series) -> float | object:
    frame = pd.DataFrame({"value": pd.to_numeric(values, errors="coerce"), "weight": pd.to_numeric(weights, errors="coerce")}).dropna()
    if frame.empty or frame["weight"].sum() == 0:
        return pd.NA
    return float((frame["value"] * frame["weight"]).sum() / frame["weight"].sum())


def _mean_or_na(values: object) -> float | object:
    series = pd.Series(values).dropna()
    if series.empty:
        return pd.NA
    return float(series.astype(float).mean())


def _std_or_na(values: object) -> float | object:
    series = pd.Series(values).dropna()
    if series.empty:
        return pd.NA
    return float(series.astype(float).std(ddof=0))


def _icir(series: pd.Series, scale: float) -> float | object:
    std = _std_or_na(series)
    mean = _mean_or_na(series)
    if pd.isna(std) or std == 0 or pd.isna(mean):
        return pd.NA
    return float(mean / std * scale)


def _periodic_annual_return(series: pd.Series) -> float | object:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return pd.NA
    return float((1.0 + values).prod() ** (12.0 / len(values)) - 1.0)


def _sharpe(series: pd.Series) -> float | object:
    annual_return = _periodic_annual_return(series)
    std = _std_or_na(series)
    if pd.isna(annual_return) or pd.isna(std) or std == 0:
        return pd.NA
    return float(annual_return / (std * math.sqrt(12.0)))


def _summarize_size_exposure_for_report(size_exposure: pd.DataFrame) -> pd.DataFrame:
    if size_exposure.empty:
        return pd.DataFrame()
    return (
        size_exposure.groupby("factor", as_index=False)
        .agg(
            average_active_log_total_mv=("active_log_total_mv", "mean"),
            average_portfolio_size_percentile=("portfolio_size_percentile", "mean"),
        )
        .sort_values("average_active_log_total_mv")
        .reset_index(drop=True)
    )


def _summarize_industry_exposure_for_report(industry_exposure: pd.DataFrame) -> pd.DataFrame:
    if industry_exposure.empty:
        return pd.DataFrame()
    frame = industry_exposure.copy()
    frame["abs_active_weight"] = frame["active_weight"].abs()
    summary = (
        frame.groupby("factor", as_index=False)
        .agg(
            average_abs_active_weight=("abs_active_weight", "mean"),
            max_abs_active_weight=("abs_active_weight", "max"),
        )
        .sort_values("average_abs_active_weight", ascending=False)
        .reset_index(drop=True)
    )
    unknown = frame[frame["industry"] == "Unknown"].groupby("factor", as_index=False).agg(unknown_active_weight=("active_weight", "mean"))
    return summary.merge(unknown, on="factor", how="left").fillna({"unknown_active_weight": 0.0})


def _format_report_table(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if frame.empty:
        return frame
    available = [col for col in columns if col in frame.columns]
    table = frame[available].copy()
    for col in table.columns:
        if col == "factor":
            table[col] = table[col].map(lambda value: FACTOR_DISPLAY_NAMES.get(value, value))
        elif pd.api.types.is_numeric_dtype(table[col]):
            table[col] = table[col].map(lambda value: "" if pd.isna(value) else f"{value:.4f}")
    return table


def _display_factor(value: object) -> str:
    text = str(value)
    return str(FACTOR_DISPLAY_NAMES.get(text, text))


def _markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return ""
    columns = [str(col) for col in frame.columns]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in frame.astype(str).itertuples(index=False):
        lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in row) + " |")
    return "\n".join(lines)
