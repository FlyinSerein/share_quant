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
        FactorResearchRunner,
        backtest_monthly_top_quantile,
        build_rebalance_calendar,
        compute_performance_metrics,
        filter_scores_for_execution_universe,
        neutralize_scores,
        normalize_factor_panel,
        select_top_quantile_weights,
        yyyymmdd,
    )
    from .factor_diagnostics import (
        assign_score_deciles,
        build_forward_period_returns,
        compute_decile_returns,
        compute_ic_by_period,
        compute_long_short_returns,
        compute_periodic_return_metrics,
        summarize_decile_returns,
        summarize_ic,
    )
except ImportError:  # pragma: no cover - supports direct script execution.
    from factor_backtest import (
        DATE_FMT,
        FactorResearchRunner,
        backtest_monthly_top_quantile,
        build_rebalance_calendar,
        compute_performance_metrics,
        filter_scores_for_execution_universe,
        neutralize_scores,
        normalize_factor_panel,
        select_top_quantile_weights,
        yyyymmdd,
    )
    from factor_diagnostics import (
        assign_score_deciles,
        build_forward_period_returns,
        compute_decile_returns,
        compute_ic_by_period,
        compute_long_short_returns,
        compute_periodic_return_metrics,
        summarize_decile_returns,
        summarize_ic,
    )


COMPOSITE_EQUAL = "Composite_Equal"
COMPOSITE_ROLLING_RANKIC = "Composite_RollingRankIC"
COMPOSITE_FACTORS = (COMPOSITE_EQUAL, COMPOSITE_ROLLING_RANKIC)


def build_composite_scores(
    scores: pd.DataFrame,
    rank_ic_by_period: pd.DataFrame | None = None,
    score_col: str = "neutralized_score",
    rankic_window: int = 12,
    rankic_min_periods: int = 6,
    min_factor_count: int = 6,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    required = {"factor", "trade_date", "ts_code", score_col}
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"scores are missing columns: {sorted(missing)}")
    if rankic_window < 1:
        raise ValueError("rankic_window must be at least 1")
    if rankic_min_periods < 1:
        raise ValueError("rankic_min_periods must be at least 1")
    if min_factor_count < 1:
        raise ValueError("min_factor_count must be at least 1")

    if scores.empty:
        empty_scores = pd.DataFrame(
            columns=["factor", "trade_date", "ts_code", "score", "available_factor_count", "weight_source"]
        )
        empty_weights = pd.DataFrame(columns=["trade_date", "composite_factor", "source_factor", "weight", "rank_ic_mean", "rank_ic_periods", "weight_source"])
        return empty_scores, empty_weights

    base = scores[["factor", "trade_date", "ts_code", score_col]].copy()
    base[score_col] = pd.to_numeric(base[score_col], errors="coerce")
    matrix = (
        base.pivot_table(index=["trade_date", "ts_code"], columns="factor", values=score_col, aggfunc="mean")
        .sort_index()
        .sort_index(axis=1)
    )
    factors = list(matrix.columns)
    if matrix.empty or not factors:
        empty_scores = pd.DataFrame(
            columns=["factor", "trade_date", "ts_code", "score", "available_factor_count", "weight_source"]
        )
        empty_weights = pd.DataFrame(columns=["trade_date", "composite_factor", "source_factor", "weight", "rank_ic_mean", "rank_ic_periods", "weight_source"])
        return empty_scores, empty_weights
    dates = sorted(matrix.index.get_level_values("trade_date").unique())
    rows: list[pd.DataFrame] = []
    weight_rows: list[dict[str, object]] = []

    for trade_date in dates:
        cross_section = matrix.xs(trade_date, level="trade_date")
        equal_weights = pd.Series(1.0 / len(factors), index=factors, dtype="float64")
        equal_scores = cross_section.mean(axis=1, skipna=True)
        valid_counts = cross_section.notna().sum(axis=1)
        equal_frame = pd.DataFrame(
            {
                "factor": COMPOSITE_EQUAL,
                "trade_date": trade_date,
                "ts_code": cross_section.index.astype(str),
                "score": equal_scores.where(valid_counts >= min_factor_count).to_numpy(),
                "available_factor_count": valid_counts.to_numpy(dtype=int),
                "weight_source": "equal",
            }
        )
        rows.append(equal_frame)
        for source_factor, weight in equal_weights.items():
            weight_rows.append(
                {
                    "trade_date": trade_date,
                    "composite_factor": COMPOSITE_EQUAL,
                    "source_factor": source_factor,
                    "weight": float(weight),
                    "rank_ic_mean": pd.NA,
                    "rank_ic_periods": 0,
                    "weight_source": "equal",
                }
            )

        rankic_weights, rankic_stats, source = _rolling_rankic_weights(
            rank_ic_by_period,
            trade_date,
            factors,
            rankic_window=rankic_window,
            rankic_min_periods=rankic_min_periods,
        )
        weighted_scores = _weighted_cross_section_score(cross_section, rankic_weights, min_factor_count)
        rolling_frame = pd.DataFrame(
            {
                "factor": COMPOSITE_ROLLING_RANKIC,
                "trade_date": trade_date,
                "ts_code": cross_section.index.astype(str),
                "score": weighted_scores.to_numpy(),
                "available_factor_count": valid_counts.to_numpy(dtype=int),
                "weight_source": source,
            }
        )
        rows.append(rolling_frame)
        for source_factor in factors:
            stats = rankic_stats.get(source_factor, {})
            weight_rows.append(
                {
                    "trade_date": trade_date,
                    "composite_factor": COMPOSITE_ROLLING_RANKIC,
                    "source_factor": source_factor,
                    "weight": float(rankic_weights.get(source_factor, 0.0)),
                    "rank_ic_mean": stats.get("rank_ic_mean", pd.NA),
                    "rank_ic_periods": int(stats.get("rank_ic_periods", 0)),
                    "weight_source": source,
                }
            )

    composite = pd.concat(rows, ignore_index=True)
    factor_weights = pd.DataFrame(weight_rows)
    composite["score"] = pd.to_numeric(composite["score"], errors="coerce")
    return composite.sort_values(["factor", "trade_date", "ts_code"]).reset_index(drop=True), factor_weights


def assign_score_layers(scores: pd.DataFrame, bucket_count: int = 10, score_col: str = "score") -> pd.DataFrame:
    required = {"factor", "trade_date", "ts_code", score_col}
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"scores are missing columns: {sorted(missing)}")
    if bucket_count < 2:
        raise ValueError("bucket_count must be at least 2")

    valid = scores.dropna(subset=[score_col]).copy()
    if valid.empty:
        return pd.DataFrame(columns=["factor", "trade_date", "ts_code", "bucket"])

    frames = []
    for (_factor, _trade_date), group in valid.groupby(["factor", "trade_date"], sort=True):
        ranked = group.sort_values([score_col, "ts_code"], ascending=[True, True]).copy()
        n = len(ranked)
        ranked["bucket"] = np.ceil(np.arange(1, n + 1) * bucket_count / n).astype(int).clip(1, bucket_count)
        frames.append(ranked[["factor", "trade_date", "ts_code", "bucket"]])
    return pd.concat(frames, ignore_index=True)


def build_layer_weights(layers: pd.DataFrame) -> pd.DataFrame:
    required = {"factor", "trade_date", "ts_code", "bucket"}
    missing = required - set(layers.columns)
    if missing:
        raise ValueError(f"layers are missing columns: {sorted(missing)}")
    if layers.empty:
        return pd.DataFrame(columns=["factor", "composite_factor", "trade_date", "ts_code", "bucket", "weight"])

    unique = layers.dropna(subset=["factor", "trade_date", "ts_code", "bucket"]).drop_duplicates(
        ["factor", "trade_date", "ts_code", "bucket"]
    )
    unique = unique.copy()
    unique["bucket"] = unique["bucket"].astype(int)
    unique["holding_count"] = unique.groupby(["factor", "trade_date", "bucket"])["ts_code"].transform("count")
    unique["weight"] = 1.0 / unique["holding_count"]
    unique["composite_factor"] = unique["factor"]
    unique["factor"] = unique.apply(lambda row: _layer_factor_name(row["composite_factor"], int(row["bucket"])), axis=1)
    return unique[["factor", "composite_factor", "trade_date", "ts_code", "bucket", "weight"]].sort_values(
        ["composite_factor", "trade_date", "bucket", "ts_code"]
    ).reset_index(drop=True)


def build_multifactor_results(
    composite_scores: pd.DataFrame,
    factor_weights: pd.DataFrame,
    returns: pd.DataFrame,
    rebalance_calendar: pd.DataFrame,
    end_date: str,
    benchmark_returns: pd.DataFrame,
    forward_returns: pd.DataFrame,
    bucket_count: int = 10,
    transaction_cost: float = 0.001,
    suspensions: pd.DataFrame | None = None,
    single_factor_top_metrics: pd.DataFrame | None = None,
    single_factor_long_short_metrics: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    layers = assign_score_layers(composite_scores, bucket_count=bucket_count)
    layer_weights = build_layer_weights(layers)
    layer_daily_returns, layer_turnover = backtest_monthly_top_quantile(
        layer_weights[["factor", "trade_date", "ts_code", "weight"]],
        returns,
        rebalance_calendar,
        end_date=end_date,
        transaction_cost=transaction_cost,
        suspensions=suspensions,
    )
    layer_daily_returns = _attach_layer_columns(layer_daily_returns)
    layer_turnover = _attach_layer_columns(layer_turnover)
    layer_metrics = compute_performance_metrics(
        _drop_layer_columns(layer_daily_returns),
        benchmark_returns,
        _drop_layer_columns(layer_turnover),
    )
    layer_metrics = _attach_layer_columns(layer_metrics)
    layer_nav = build_layer_nav(layer_daily_returns, benchmark_returns)

    deciles = layers.rename(columns={"bucket": "decile"})
    decile_returns = compute_decile_returns(deciles, forward_returns)
    decile_summary = summarize_decile_returns(decile_returns, bucket_count)
    long_short_returns = compute_long_short_returns(decile_returns, bucket_count)
    long_short_metrics = compute_periodic_return_metrics(long_short_returns, "long_short_return")
    composite_ic_by_period = compute_ic_by_period(composite_scores, forward_returns, score_col="score")
    composite_ic_summary = summarize_ic(composite_ic_by_period)

    layer_period_returns = decile_returns.rename(columns={"factor": "composite_factor", "decile": "bucket"})
    layer_summary = decile_summary.rename(columns={"factor": "composite_factor"})
    long_short_returns = long_short_returns.rename(columns={"factor": "composite_factor"})
    long_short_metrics = long_short_metrics.rename(columns={"factor": "composite_factor"})
    composite_ic_by_period = composite_ic_by_period.rename(columns={"factor": "composite_factor"})
    composite_ic_summary = composite_ic_summary.rename(columns={"factor": "composite_factor"})

    return {
        "composite_scores": composite_scores,
        "composite_factor_weights": factor_weights,
        "composite_scores_coverage": composite_score_coverage(composite_scores),
        "layers": layers,
        "layer_weights": layer_weights,
        "top_layer_weights": layer_weights[layer_weights["bucket"] == bucket_count].reset_index(drop=True),
        "layer_daily_returns": layer_daily_returns,
        "layer_turnover": layer_turnover,
        "layer_metrics": layer_metrics,
        "layer_nav": layer_nav,
        "layer_period_returns": layer_period_returns,
        "layer_summary": layer_summary,
        "long_short_returns": long_short_returns,
        "long_short_metrics": long_short_metrics,
        "composite_ic_by_period": composite_ic_by_period,
        "composite_ic_summary": composite_ic_summary,
        "single_factor_top_metrics": single_factor_top_metrics if single_factor_top_metrics is not None else pd.DataFrame(),
        "single_factor_long_short_metrics": (
            single_factor_long_short_metrics if single_factor_long_short_metrics is not None else pd.DataFrame()
        ),
    }


def build_single_factor_comparison(
    scores: pd.DataFrame,
    returns: pd.DataFrame,
    rebalance_calendar: pd.DataFrame,
    end_date: str,
    benchmark_returns: pd.DataFrame,
    forward_returns: pd.DataFrame,
    bucket_count: int = 10,
    transaction_cost: float = 0.001,
    suspensions: pd.DataFrame | None = None,
    score_col: str = "neutralized_score",
) -> dict[str, pd.DataFrame]:
    if scores.empty:
        return {
            "single_factor_top_metrics": pd.DataFrame(),
            "single_factor_long_short_metrics": pd.DataFrame(),
        }

    top_weights = select_top_quantile_weights(scores, quantile=0.2, score_col=score_col)
    top_daily_returns, top_turnover = backtest_monthly_top_quantile(
        top_weights,
        returns,
        rebalance_calendar,
        end_date=end_date,
        transaction_cost=transaction_cost,
        suspensions=suspensions,
    )
    top_metrics = compute_performance_metrics(top_daily_returns, benchmark_returns, top_turnover)

    deciles = assign_score_deciles(scores, bucket_count=bucket_count, score_col=score_col)
    decile_returns = compute_decile_returns(deciles, forward_returns)
    long_short_returns = compute_long_short_returns(decile_returns, bucket_count=bucket_count)
    long_short_metrics = compute_periodic_return_metrics(long_short_returns, "long_short_return")
    return {
        "single_factor_top_metrics": top_metrics,
        "single_factor_long_short_metrics": long_short_metrics,
    }


def composite_score_coverage(composite_scores: pd.DataFrame) -> pd.DataFrame:
    if composite_scores.empty:
        return pd.DataFrame()
    frame = composite_scores.copy()
    frame["is_valid"] = frame["score"].notna()
    by_date = (
        frame.groupby(["factor", "trade_date"], as_index=False)
        .agg(
            valid_stock_count=("is_valid", "sum"),
            average_available_factor_count=("available_factor_count", "mean"),
        )
    )
    return (
        by_date.groupby("factor", as_index=False)
        .agg(
            signal_count=("trade_date", "nunique"),
            first_signal=("trade_date", "min"),
            last_signal=("trade_date", "max"),
            average_valid_stocks=("valid_stock_count", "mean"),
            min_valid_stocks=("valid_stock_count", "min"),
            average_available_factor_count=("average_available_factor_count", "mean"),
        )
        .sort_values("factor")
        .reset_index(drop=True)
    )


def build_layer_nav(layer_daily_returns: pd.DataFrame, benchmark_returns: pd.DataFrame) -> pd.DataFrame:
    if layer_daily_returns.empty:
        return pd.DataFrame(columns=["factor", "composite_factor", "bucket", "trade_date", "daily_return", "nav"])
    frames = []
    for factor, group in layer_daily_returns.groupby("factor", sort=True):
        temp = group.sort_values("trade_date").copy()
        temp["daily_return"] = pd.to_numeric(temp["portfolio_return"], errors="coerce").fillna(0.0)
        temp["nav"] = (1.0 + temp["daily_return"]).cumprod()
        frames.append(temp[["factor", "composite_factor", "bucket", "trade_date", "daily_return", "nav"]])
    if benchmark_returns.empty:
        return pd.concat(frames, ignore_index=True)
    first_date = layer_daily_returns["trade_date"].min()
    benchmark = benchmark_returns[benchmark_returns["trade_date"] >= first_date].copy()
    benchmark["factor"] = "Benchmark_000985_CSI"
    benchmark["composite_factor"] = "Benchmark_000985_CSI"
    benchmark["bucket"] = pd.NA
    benchmark["daily_return"] = pd.to_numeric(benchmark["benchmark_return"], errors="coerce").fillna(0.0)
    benchmark["nav"] = (1.0 + benchmark["daily_return"]).cumprod()
    frames.append(benchmark[["factor", "composite_factor", "bucket", "trade_date", "daily_return", "nav"]])
    return pd.concat(frames, ignore_index=True)


class MultifactorLayeredBacktestRunner:
    def __init__(
        self,
        project_root: Path,
        db_path: Path,
        output_dir: Path,
        start: str = "2022-01-01",
        end: str | None = None,
        warmup_start: str = "2021-01-01",
        benchmark: str = "000985.CSI",
        transaction_cost: float = 0.001,
        bucket_count: int = 10,
        rankic_window: int = 12,
        rankic_min_periods: int = 6,
        min_factor_count: int = 6,
    ) -> None:
        self.project_root = Path(project_root)
        self.db_path = Path(db_path)
        self.output_dir = Path(output_dir)
        self.start = yyyymmdd(start)
        self.end = yyyymmdd(end) if end else None
        self.warmup_start = yyyymmdd(warmup_start)
        self.benchmark = benchmark
        self.transaction_cost = transaction_cost
        self.bucket_count = bucket_count
        self.rankic_window = rankic_window
        self.rankic_min_periods = rankic_min_periods
        self.min_factor_count = min_factor_count
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
        (self.output_dir / "tables").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "images").mkdir(parents=True, exist_ok=True)

        with duckdb.connect(str(self.db_path), read_only=True) as con:
            end_date = self.end or self._research._latest_trade_date(con)
            all_trade_dates = self._research._load_trade_dates(con, self.warmup_start, end_date)
            rebalance_calendar = build_rebalance_calendar(all_trade_dates, self.start, end_date)
            signal_dates = rebalance_calendar["signal_date"].drop_duplicates().reset_index(drop=True)
            con.register("signal_dates", pd.DataFrame({"trade_date": signal_dates}))

            raw_factors = self._research._load_factor_panel(con, signal_dates, end_date)
            scores = normalize_factor_panel(raw_factors)
            execution_universe = self._research._load_execution_universe(con, rebalance_calendar)
            returns = self._research._load_returns(con, self.start, end_date)
            suspensions = self._research._load_suspensions(con, self.start, end_date)
            benchmark_returns = self._research._load_benchmark(con, self.warmup_start, end_date)
            exposures = self._research._load_exposures(con, signal_dates)

        neutralized_scores = neutralize_scores(scores, exposures)
        tradable_scores = filter_scores_for_execution_universe(neutralized_scores, rebalance_calendar, execution_universe)
        forward_returns = build_forward_period_returns(
            returns,
            rebalance_calendar,
            end_date,
            ts_codes=tradable_scores["ts_code"].dropna().unique(),
            suspensions=suspensions,
        )
        rank_ic_by_period = compute_ic_by_period(tradable_scores, forward_returns, score_col="neutralized_score")
        composite_scores, factor_weights = build_composite_scores(
            tradable_scores,
            rank_ic_by_period=rank_ic_by_period,
            score_col="neutralized_score",
            rankic_window=self.rankic_window,
            rankic_min_periods=self.rankic_min_periods,
            min_factor_count=self.min_factor_count,
        )
        single_factor_comparison = build_single_factor_comparison(
            scores=tradable_scores,
            returns=returns,
            rebalance_calendar=rebalance_calendar,
            end_date=end_date,
            benchmark_returns=benchmark_returns,
            forward_returns=forward_returns,
            bucket_count=self.bucket_count,
            transaction_cost=self.transaction_cost,
            suspensions=suspensions,
            score_col="neutralized_score",
        )
        results = build_multifactor_results(
            composite_scores=composite_scores,
            factor_weights=factor_weights,
            returns=returns,
            rebalance_calendar=rebalance_calendar,
            end_date=end_date,
            benchmark_returns=benchmark_returns,
            forward_returns=forward_returns,
            bucket_count=self.bucket_count,
            transaction_cost=self.transaction_cost,
            suspensions=suspensions,
            single_factor_top_metrics=single_factor_comparison["single_factor_top_metrics"],
            single_factor_long_short_metrics=single_factor_comparison["single_factor_long_short_metrics"],
        )
        return self._write_outputs(results, end_date=end_date)

    def _write_outputs(self, results: dict[str, pd.DataFrame], end_date: str) -> dict[str, Path]:
        tables = self.output_dir / "tables"
        images = self.output_dir / "images"
        paths = {
            "composite_factor_weights": tables / "composite_factor_weights.csv",
            "composite_scores_coverage": tables / "composite_scores_coverage.csv",
            "composite_scores": tables / "composite_scores.csv",
            "layer_daily_returns": tables / "layer_daily_returns.csv",
            "layer_metrics": tables / "layer_metrics.csv",
            "layer_nav": tables / "layer_nav.csv",
            "layer_period_returns": tables / "layer_period_returns.csv",
            "layer_summary": tables / "layer_summary.csv",
            "long_short_returns": tables / "long_short_returns.csv",
            "long_short_metrics": tables / "long_short_metrics.csv",
            "top_layer_weights": tables / "top_layer_weights.csv",
            "layer_turnover": tables / "layer_turnover.csv",
            "composite_ic_by_period": tables / "composite_ic_by_period.csv",
            "composite_ic_summary": tables / "composite_ic_summary.csv",
            "single_factor_top_metrics": tables / "single_factor_top_metrics.csv",
            "single_factor_long_short_metrics": tables / "single_factor_long_short_metrics.csv",
            "report": self.output_dir / "report.md",
        }
        for key, path in paths.items():
            if key == "report":
                continue
            results[key].to_csv(path, index=False, encoding="utf-8-sig")

        image_paths = self._write_images(results, images)
        paths.update(image_paths)
        self._write_report(paths["report"], results, end_date=end_date, image_paths=image_paths)
        return paths

    def _write_images(self, results: dict[str, pd.DataFrame], images: Path) -> dict[str, Path]:
        try:
            import matplotlib.pyplot as plt
            from matplotlib.ticker import PercentFormatter
        except ImportError:
            return {}

        paths: dict[str, Path] = {}
        plt.style.use("seaborn-v0_8-whitegrid")

        period_returns = results["layer_period_returns"]
        if not period_returns.empty:
            means = (
                period_returns.groupby(["composite_factor", "bucket"], as_index=False)["average_forward_return"]
                .mean()
                .sort_values(["composite_factor", "bucket"])
            )
            path = images / "layer_period_returns.png"
            fig, ax = plt.subplots(figsize=(10, 6))
            for factor, group in means.groupby("composite_factor", sort=True):
                ax.plot(group["bucket"], group["average_forward_return"], marker="o", linewidth=1.8, label=factor)
            ax.axhline(0, color="#333333", linewidth=0.8)
            ax.set_title("Composite Factor Layer Returns")
            ax.set_xlabel("Bucket (highest score is max bucket)")
            ax.set_ylabel("Average Holding-Period Return")
            ax.yaxis.set_major_formatter(PercentFormatter(1.0))
            ax.legend(frameon=False)
            fig.tight_layout()
            fig.savefig(path, dpi=170)
            plt.close(fig)
            paths["layer_period_returns_image"] = path

        long_short = results["long_short_returns"]
        if not long_short.empty:
            path = images / "long_short_nav.png"
            fig, ax = plt.subplots(figsize=(10, 6))
            for factor, group in long_short.groupby("composite_factor", sort=True):
                group = group.sort_values("trade_date")
                dates = pd.to_datetime(group["trade_date"], format=DATE_FMT)
                nav = (1.0 + pd.to_numeric(group["long_short_return"], errors="coerce").fillna(0.0)).cumprod()
                ax.plot(dates, nav, linewidth=1.8, label=factor)
            ax.set_title("D10 - D1 Long/Short NAV")
            ax.set_xlabel("Date")
            ax.set_ylabel("NAV")
            ax.legend(frameon=False)
            fig.tight_layout()
            fig.savefig(path, dpi=170)
            plt.close(fig)
            paths["long_short_nav_image"] = path

        comparison_path = self._write_single_vs_multifactor_image(results, images, plt, PercentFormatter)
        if comparison_path is not None:
            paths["single_vs_multifactor_image"] = comparison_path

        weights = results["composite_factor_weights"]
        rankic_weights = weights[weights["composite_factor"] == COMPOSITE_ROLLING_RANKIC]
        if not rankic_weights.empty:
            path = images / "rankic_weights.png"
            pivot = rankic_weights.pivot_table(index="trade_date", columns="source_factor", values="weight", aggfunc="mean").sort_index()
            fig, ax = plt.subplots(figsize=(12, 6.5))
            dates = pd.to_datetime(pivot.index, format=DATE_FMT)
            for col in pivot.columns:
                ax.plot(dates, pivot[col], linewidth=1.0, alpha=0.8, label=col)
            ax.set_title("Rolling RankIC Composite Weights")
            ax.set_xlabel("Date")
            ax.set_ylabel("Weight")
            ax.yaxis.set_major_formatter(PercentFormatter(1.0))
            ax.legend(fontsize=7, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.12), frameon=False)
            fig.tight_layout(rect=(0, 0.08, 1, 1))
            fig.savefig(path, dpi=170)
            plt.close(fig)
            paths["rankic_weights_image"] = path

        return paths

    def _write_single_vs_multifactor_image(self, results: dict[str, pd.DataFrame], images: Path, plt: object, percent_formatter: object) -> Path | None:
        single_top = results["single_factor_top_metrics"]
        single_ls = results["single_factor_long_short_metrics"]
        layer_metrics = results["layer_metrics"]
        multi_ls = results["long_short_metrics"]
        if single_top.empty and single_ls.empty:
            return None

        top_rows = []
        if not single_top.empty:
            for row in single_top.sort_values("annual_return", ascending=False).to_dict("records"):
                top_rows.append({"label": str(row["factor"]), "annual_return": row["annual_return"], "kind": "Single"})
        top_multi = layer_metrics[layer_metrics["bucket"] == self.bucket_count] if not layer_metrics.empty else pd.DataFrame()
        for row in top_multi.sort_values("annual_return", ascending=False).to_dict("records"):
            top_rows.append({"label": str(row["composite_factor"]), "annual_return": row["annual_return"], "kind": "Composite"})

        long_short_rows = []
        if not single_ls.empty:
            for row in single_ls.sort_values("annual_return", ascending=False).to_dict("records"):
                long_short_rows.append({"label": str(row["factor"]), "annual_return": row["annual_return"], "kind": "Single"})
        for row in multi_ls.sort_values("annual_return", ascending=False).to_dict("records"):
            long_short_rows.append({"label": str(row["composite_factor"]), "annual_return": row["annual_return"], "kind": "Composite"})

        path = images / "single_vs_multifactor.png"
        fig, axes = plt.subplots(1, 2, figsize=(16, 7))
        _plot_comparison_bars(
            axes[0],
            pd.DataFrame(top_rows),
            title=f"Top Portfolio Annual Return (Single Top20% vs Composite D{self.bucket_count})",
            percent_formatter=percent_formatter,
        )
        _plot_comparison_bars(
            axes[1],
            pd.DataFrame(long_short_rows),
            title=f"Long/Short Annual Return (D{self.bucket_count} - D1)",
            percent_formatter=percent_formatter,
        )
        fig.tight_layout()
        fig.savefig(path, dpi=170)
        plt.close(fig)
        return path

    def _write_report(
        self,
        path: Path,
        results: dict[str, pd.DataFrame],
        end_date: str,
        image_paths: dict[str, Path],
    ) -> None:
        coverage = _format_report_numbers(results["composite_scores_coverage"])
        top_metrics = results["layer_metrics"]
        top_metrics = top_metrics[top_metrics["bucket"] == self.bucket_count] if not top_metrics.empty else pd.DataFrame()
        long_short_metrics = _format_report_numbers(results["long_short_metrics"])
        composite_ic = _format_report_numbers(results["composite_ic_summary"])
        image_lines = "\n".join(f"- `{p.relative_to(path.parent).as_posix()}`" for p in image_paths.values())
        content = f"""# Multifactor Layered Backtest Report

## Sample and Method

- Output directory: `{path.parent.as_posix()}/`.
- Evaluation window: `{self.start}` to `{end_date}`; warmup starts at `{self.warmup_start}`.
- Input scores: industry and size neutralized single-factor scores.
- Composite factors: `{COMPOSITE_EQUAL}` and `{COMPOSITE_ROLLING_RANKIC}`.
- Rolling RankIC weights: window `{self.rankic_window}`, min periods `{self.rankic_min_periods}`, negative means clipped to zero, fallback to equal weight when history is insufficient or non-positive.
- Minimum valid factor count per stock: `{self.min_factor_count}`.
- Layers: `{self.bucket_count}` buckets, highest score is D{self.bucket_count}.
- Signal and execution: month-end signal, next trading day close execution, held until next rebalance.
- Transaction cost: one-way `{self.transaction_cost:.2%}` deducted by layer turnover.
- Benchmark: `{self.benchmark}`.

## Composite Coverage

{_markdown_table(coverage)}

## Top Layer Metrics

{_markdown_table(_format_report_numbers(top_metrics))}

## D{self.bucket_count} - D1 Long/Short Metrics

{_markdown_table(long_short_metrics)}

## Composite RankIC

{_markdown_table(composite_ic)}

## Output Files

- Tables: `tables/composite_factor_weights.csv`, `tables/composite_scores_coverage.csv`, `tables/layer_metrics.csv`, `tables/layer_nav.csv`, `tables/layer_period_returns.csv`, `tables/long_short_metrics.csv`, `tables/top_layer_weights.csv`
- Extra diagnostics: `tables/composite_scores.csv`, `tables/layer_daily_returns.csv`, `tables/layer_turnover.csv`, `tables/composite_ic_by_period.csv`, `tables/composite_ic_summary.csv`
- Single-factor comparison: `tables/single_factor_top_metrics.csv`, `tables/single_factor_long_short_metrics.csv`

## Images

{image_lines}
"""
        path.write_text(content, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run multifactor composite layered backtests.")
    parser.add_argument("--start", default="2022-01-01", help="Evaluation start date, YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="Evaluation end date, YYYY-MM-DD. Defaults to latest local trade date.")
    parser.add_argument("--warmup-start", default="2021-01-01", help="Warmup start date for rolling factors.")
    parser.add_argument("--benchmark", default="000985.CSI", help="Benchmark index code.")
    parser.add_argument("--transaction-cost", type=float, default=0.001, help="One-way transaction cost.")
    parser.add_argument("--bucket-count", type=int, default=10, help="Number of score layers.")
    parser.add_argument("--rankic-window", type=int, default=12, help="Rolling RankIC lookback periods.")
    parser.add_argument("--rankic-min-periods", type=int, default=6, help="Minimum historical RankIC periods per source factor.")
    parser.add_argument("--min-factor-count", type=int, default=6, help="Minimum valid source factors per stock.")
    parser.add_argument("--output-dir", default=None, help="Output directory. Defaults to outputs/multifactor/.")
    return parser


def run_from_args(argv: list[str] | None = None) -> dict[str, Path]:
    args = build_parser().parse_args(argv)
    here = Path(__file__).resolve().parent
    project_root = here.parents[1]
    output_dir = Path(args.output_dir).resolve() if args.output_dir else here / "outputs" / "multifactor"
    runner = MultifactorLayeredBacktestRunner(
        project_root=project_root,
        db_path=project_root / "data" / "share_quant.duckdb",
        output_dir=output_dir,
        start=args.start,
        end=args.end,
        warmup_start=args.warmup_start,
        benchmark=args.benchmark,
        transaction_cost=args.transaction_cost,
        bucket_count=args.bucket_count,
        rankic_window=args.rankic_window,
        rankic_min_periods=args.rankic_min_periods,
        min_factor_count=args.min_factor_count,
    )
    return runner.run()


def _rolling_rankic_weights(
    rank_ic_by_period: pd.DataFrame | None,
    trade_date: str,
    factors: list[str],
    rankic_window: int,
    rankic_min_periods: int,
) -> tuple[pd.Series, dict[str, dict[str, object]], str]:
    equal = pd.Series(1.0 / len(factors), index=factors, dtype="float64")
    if rank_ic_by_period is None or rank_ic_by_period.empty:
        return equal, {}, "fallback_equal"
    required = {"factor", "trade_date", "rank_ic"}
    if required - set(rank_ic_by_period.columns):
        raise ValueError("rank_ic_by_period must include factor, trade_date, rank_ic")

    history = rank_ic_by_period[rank_ic_by_period["trade_date"].astype(str) < str(trade_date)].copy()
    if history.empty:
        return equal, {}, "fallback_equal"

    raw_weights: dict[str, float] = {}
    stats: dict[str, dict[str, object]] = {}
    for factor in factors:
        series = (
            history[history["factor"] == factor]
            .sort_values("trade_date")["rank_ic"]
            .pipe(pd.to_numeric, errors="coerce")
            .dropna()
            .tail(rankic_window)
        )
        stats[factor] = {
            "rank_ic_mean": float(series.mean()) if len(series) else pd.NA,
            "rank_ic_periods": int(len(series)),
        }
        if len(series) >= rankic_min_periods:
            raw_weights[factor] = max(float(series.mean()), 0.0)
        else:
            raw_weights[factor] = 0.0

    weights = pd.Series(raw_weights, index=factors, dtype="float64")
    if weights.sum() <= 0:
        return equal, stats, "fallback_equal"
    return weights / weights.sum(), stats, "rolling_rankic"


def _weighted_cross_section_score(cross_section: pd.DataFrame, weights: pd.Series, min_factor_count: int) -> pd.Series:
    aligned_weights = weights.reindex(cross_section.columns).fillna(0.0).astype(float)
    valid = cross_section.notna()
    valid_counts = valid.sum(axis=1)
    available_weight_sum = valid.mul(aligned_weights, axis=1).sum(axis=1)
    weighted_sum = cross_section.fillna(0.0).mul(aligned_weights, axis=1).sum(axis=1)
    score = weighted_sum.div(available_weight_sum).where(available_weight_sum > 0)
    return score.where(valid_counts >= min_factor_count)


def _layer_factor_name(composite_factor: object, bucket: int) -> str:
    return f"{composite_factor}_D{bucket:02d}"


def _split_layer_factor(factor: object) -> tuple[str, int | object]:
    text = str(factor)
    if "_D" not in text:
        return text, pd.NA
    base, suffix = text.rsplit("_D", 1)
    try:
        return base, int(suffix)
    except ValueError:
        return text, pd.NA


def _attach_layer_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "factor" not in frame.columns:
        return frame
    result = frame.copy()
    parsed = result["factor"].map(_split_layer_factor)
    result["composite_factor"] = parsed.map(lambda value: value[0])
    result["bucket"] = parsed.map(lambda value: value[1])
    return result


def _drop_layer_columns(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.drop(columns=[col for col in ["composite_factor", "bucket"] if col in frame.columns])


def _format_report_numbers(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    table = frame.copy()
    for col in table.columns:
        if pd.api.types.is_numeric_dtype(table[col]):
            if col in {
                "bucket",
                "period_count",
                "signal_count",
                "rank_ic_periods",
                "trading_days",
                "missing_return_count",
                "suspended_return_count",
                "invalid_missing_return_count",
            }:
                table[col] = table[col].map(lambda value: "" if pd.isna(value) else f"{value:.0f}")
            elif any(token in col for token in ["return", "volatility", "drawdown", "weight", "rate", "turnover"]):
                table[col] = table[col].map(lambda value: "" if pd.isna(value) else f"{value:.2%}")
            else:
                table[col] = table[col].map(lambda value: "" if pd.isna(value) else f"{value:.4f}")
    return table


def _plot_comparison_bars(ax: object, frame: pd.DataFrame, title: str, percent_formatter: object) -> None:
    if frame.empty:
        ax.axis("off")
        ax.set_title(title)
        return
    table = frame.copy()
    table["annual_return"] = pd.to_numeric(table["annual_return"], errors="coerce")
    table = table.dropna(subset=["annual_return"]).sort_values("annual_return", ascending=True)
    colors = table["kind"].map({"Single": "#4c78a8", "Composite": "#f58518"}).fillna("#777777")
    ax.barh(table["label"], table["annual_return"], color=colors)
    ax.axvline(0, color="#333333", linewidth=0.8)
    ax.set_title(title)
    ax.set_xlabel("Annual Return")
    ax.xaxis.set_major_formatter(percent_formatter(1.0))
    for label in ax.get_yticklabels():
        if table.loc[table["label"].eq(label.get_text()), "kind"].eq("Composite").any():
            label.set_weight("bold")


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
