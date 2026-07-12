from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import duckdb
import numpy as np
import pandas as pd


DATE_FMT = "%Y%m%d"
FACTOR_DISPLAY_NAMES = {
    "Benchmark_000985_CSI": "Benchmark",
    "ROE": "ROE",
    "PE_TTM": "PE TTM",
    "Revenue_Growth": "Revenue Growth",
    "Momentum_60D": "Momentum 60D",
    "Turnover_20D": "Turnover 20D",
    "Debt_to_Equity": "Debt/Equity",
    "Volatility": "Volatility",
    "Main_Net_In": "Main Net In",
    "Dividend_Yield": "Dividend Yield",
    "Holder_Concen": "Holder Concentration",
    "Gross_Margin": "Gross Margin",
}


@dataclass(frozen=True)
class FactorSpec:
    name: str
    category: str
    chinese_name: str
    formula: str
    direction: int = 1


FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec("ROE", "财务质量", "净资产收益率", "净利润/净资产", 1),
    FactorSpec("PE_TTM", "估值指标", "市盈率（TTM）", "总市值/最近12个月净利润", -1),
    FactorSpec("Revenue_Growth", "成长能力", "营收增长率", "（本期营收 - 上期营收）/上期营收", 1),
    FactorSpec("Momentum_60D", "技术动量", "60日动量因子", "收盘价（当前）/收盘价（60日前） - 1", 1),
    FactorSpec("Turnover_20D", "流动性", "换手率（20日均值）", "过去20日平均每日成交量/流通股数", 1),
    FactorSpec("Debt_to_Equity", "质量指标", "负债率", "总负债/净资产", -1),
    FactorSpec("Volatility", "波动率", "年化波动率", "过去252个交易日收益率标准差 * sqrt(252)", -1),
    FactorSpec("Main_Net_In", "资金流向", "主力资金净流入", "主力买入金额 - 主力卖出金额", 1),
    FactorSpec("Dividend_Yield", "分红能力", "股息率", "最近一年分红/当前股价", 1),
    FactorSpec("Holder_Concen", "市场情绪", "筹码集中度", "前十大股东持股比例之和", 1),
    FactorSpec("Gross_Margin", "盈利能力", "毛利率", "（营业收入 - 营业成本）/营业收入", 1),
)


def yyyymmdd(value: str) -> str:
    return value.replace("-", "")


def winsorize_zscore(values: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce").replace([math.inf, -math.inf], pd.NA)
    valid = numeric.dropna()
    if valid.empty:
        return pd.Series(pd.NA, index=values.index, dtype="Float64")
    lo = valid.quantile(lower)
    hi = valid.quantile(upper)
    clipped = numeric.clip(lower=lo, upper=hi)
    std = clipped.std(ddof=0)
    if pd.isna(std) or std == 0:
        return pd.Series(0.0, index=values.index, dtype="float64").where(clipped.notna(), pd.NA)
    return (clipped - clipped.mean()) / std


def normalize_factor_panel(panel: pd.DataFrame, specs: Iterable[FactorSpec] = FACTOR_SPECS) -> pd.DataFrame:
    directions = {spec.name: spec.direction for spec in specs}
    required = {"factor", "trade_date", "ts_code", "raw_value"}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"factor panel is missing columns: {sorted(missing)}")
    if panel.empty:
        return panel.assign(score=pd.Series(dtype="float64"))

    normalized = panel.copy()
    normalized["raw_value"] = pd.to_numeric(normalized["raw_value"], errors="coerce")
    normalized["direction"] = normalized["factor"].map(directions).fillna(1)
    normalized["directed_value"] = normalized["raw_value"] * normalized["direction"]
    normalized["score"] = (
        normalized.groupby(["factor", "trade_date"], group_keys=False)["directed_value"]
        .apply(winsorize_zscore)
        .astype("float64")
    )
    return normalized.drop(columns=["direction", "directed_value"])


def neutralize_scores(scores: pd.DataFrame, exposures: pd.DataFrame, score_col: str = "score") -> pd.DataFrame:
    required_scores = {"factor", "trade_date", "ts_code", score_col}
    required_exposures = {"trade_date", "ts_code", "industry", "log_total_mv"}
    if required_scores - set(scores.columns):
        raise ValueError(f"scores must include factor, trade_date, ts_code, {score_col}")
    if required_exposures - set(exposures.columns):
        raise ValueError("exposures must include trade_date, ts_code, industry, log_total_mv")
    if scores.empty:
        return scores.assign(neutralized_score=pd.Series(dtype="float64"))

    merged = scores.merge(exposures, on=["trade_date", "ts_code"], how="left")
    merged["industry"] = merged["industry"].fillna("Unknown")
    merged["log_total_mv"] = pd.to_numeric(merged["log_total_mv"], errors="coerce")
    frames = []
    for (_factor, _trade_date), group in merged.groupby(["factor", "trade_date"], sort=True):
        temp = group.copy()
        y = pd.to_numeric(temp[score_col], errors="coerce")
        valid_mask = y.notna()
        temp["neutralized_score"] = pd.NA
        if valid_mask.sum() < 3 or y[valid_mask].nunique() <= 1:
            frames.append(temp)
            continue
        work = temp.loc[valid_mask].copy()
        size = work["log_total_mv"].astype(float)
        size = size.fillna(size.mean() if size.notna().any() else 0.0)
        industry_dummies = pd.get_dummies(work["industry"].fillna("Unknown"), prefix="industry", drop_first=True, dtype=float)
        x = pd.concat(
            [
                pd.Series(1.0, index=work.index, name="intercept"),
                size.rename("log_total_mv"),
                industry_dummies,
            ],
            axis=1,
        )
        beta, *_ = np.linalg.lstsq(x.to_numpy(dtype=float), y.loc[work.index].to_numpy(dtype=float), rcond=None)
        residual = y.loc[work.index].to_numpy(dtype=float) - x.to_numpy(dtype=float).dot(beta)
        temp.loc[work.index, "neutralized_score"] = residual
        frames.append(temp)
    return pd.concat(frames, ignore_index=True)


def select_top_quantile_weights(
    scores: pd.DataFrame,
    quantile: float = 0.2,
    score_col: str = "score",
) -> pd.DataFrame:
    required = {"factor", "trade_date", "ts_code", score_col}
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"scores are missing columns: {sorted(missing)}")
    if not 0 < quantile <= 1:
        raise ValueError("quantile must be in (0, 1]")

    unique_scores = (
        scores.dropna(subset=[score_col])
        .groupby(["factor", "trade_date", "ts_code"], as_index=False)
        .agg({score_col: "mean"})
    )
    frames: list[pd.DataFrame] = []
    for (factor, trade_date), group in unique_scores.groupby(["factor", "trade_date"]):
        ranked = group.sort_values(score_col, ascending=False)
        count = max(1, math.ceil(len(ranked) * quantile))
        selected = ranked.head(count).copy()
        selected["weight"] = 1.0 / count
        frames.append(selected[["factor", "trade_date", "ts_code", "weight"]])
    if not frames:
        return pd.DataFrame(columns=["factor", "trade_date", "ts_code", "weight"])
    return pd.concat(frames, ignore_index=True)


def filter_scores_for_execution_universe(
    scores: pd.DataFrame,
    rebalance_calendar: pd.DataFrame,
    execution_universe: pd.DataFrame,
) -> pd.DataFrame:
    required_scores = {"factor", "trade_date", "ts_code"}
    required_calendar = {"signal_date", "exec_date"}
    required_universe = {"exec_date", "ts_code"}
    if required_scores - set(scores.columns):
        raise ValueError("scores must include factor, trade_date, ts_code")
    if required_calendar - set(rebalance_calendar.columns):
        raise ValueError("rebalance_calendar must include signal_date, exec_date")
    if required_universe - set(execution_universe.columns):
        raise ValueError("execution_universe must include exec_date, ts_code")
    if scores.empty or rebalance_calendar.empty or execution_universe.empty:
        return scores.iloc[0:0].copy()

    score_columns = scores.columns.tolist()
    calendar = rebalance_calendar[["signal_date", "exec_date"]].dropna().drop_duplicates()
    eligible = execution_universe[["exec_date", "ts_code"]].dropna().drop_duplicates()
    filtered = (
        scores.merge(calendar, left_on="trade_date", right_on="signal_date", how="inner")
        .merge(eligible, on=["exec_date", "ts_code"], how="inner")
    )
    return filtered[score_columns].reset_index(drop=True)


def one_way_turnover(previous: pd.Series | None, current: pd.Series) -> float:
    current = current.astype(float).groupby(level=0).sum()
    if previous is None:
        return 1.0 if not current.empty else 0.0
    previous = previous.astype(float).groupby(level=0).sum()
    aligned = pd.concat([previous.rename("previous"), current.rename("current")], axis=1).fillna(0.0)
    return float(aligned["current"].sub(aligned["previous"]).abs().sum() / 2.0)


def build_suspension_matrix(
    suspensions: pd.DataFrame | None,
    trade_dates: Iterable[str],
    ts_codes: Iterable[str],
) -> pd.DataFrame:
    dates = pd.Index([str(value) for value in trade_dates], name="trade_date")
    codes = pd.Index([str(value) for value in ts_codes], name="ts_code")
    matrix = pd.DataFrame(False, index=dates, columns=codes)
    if suspensions is None or suspensions.empty or dates.empty or codes.empty:
        return matrix
    required = {"trade_date", "ts_code"}
    if required - set(suspensions.columns):
        raise ValueError("suspensions must include trade_date, ts_code")

    frame = suspensions[["trade_date", "ts_code"]].dropna().drop_duplicates().copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["ts_code"] = frame["ts_code"].astype(str)
    frame = frame[frame["trade_date"].isin(dates) & frame["ts_code"].isin(codes)]
    if frame.empty:
        return matrix

    suspended = pd.crosstab(frame["trade_date"], frame["ts_code"]).astype(bool)
    matrix.update(suspended)
    return matrix.astype(bool)


def build_rebalance_calendar(trade_dates: pd.Series, start: str, end: str) -> pd.DataFrame:
    dates = pd.Series(sorted(trade_dates.astype(str).unique()), name="trade_date")
    dates = dates[(dates >= start) & (dates <= end)].reset_index(drop=True)
    if dates.empty:
        return pd.DataFrame(columns=["signal_date", "exec_date"])

    frame = pd.DataFrame({"trade_date": dates})
    frame["month"] = pd.to_datetime(frame["trade_date"], format=DATE_FMT).dt.to_period("M")
    signal_dates = frame.groupby("month", as_index=False)["trade_date"].max()["trade_date"].tolist()
    all_dates = pd.Series(sorted(trade_dates.astype(str).unique()))
    rows = []
    for signal_date in signal_dates:
        future_dates = all_dates[all_dates > signal_date]
        if future_dates.empty:
            continue
        exec_date = str(future_dates.iloc[0])
        if exec_date <= end:
            rows.append({"signal_date": signal_date, "exec_date": exec_date})
    return pd.DataFrame(rows)


def backtest_monthly_top_quantile(
    weights: pd.DataFrame,
    returns: pd.DataFrame,
    rebalance_calendar: pd.DataFrame,
    end_date: str,
    transaction_cost: float = 0.001,
    suspensions: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    required_weights = {"factor", "trade_date", "ts_code", "weight"}
    required_returns = {"trade_date", "ts_code", "return_adjusted"}
    if required_weights - set(weights.columns):
        raise ValueError("weights must include factor, trade_date, ts_code, weight")
    if required_returns - set(returns.columns):
        raise ValueError("returns must include trade_date, ts_code, return_adjusted")

    if weights.empty or rebalance_calendar.empty:
        empty_daily = pd.DataFrame(
            columns=[
                "factor",
                "trade_date",
                "portfolio_return",
                "turnover",
                "missing_return_count",
                "suspended_return_count",
                "invalid_missing_return_count",
                "invalid_missing_weight",
            ]
        )
        empty_turnover = pd.DataFrame(columns=["factor", "signal_date", "exec_date", "turnover", "holding_count"])
        return empty_daily, empty_turnover

    return_matrix = (
        returns.assign(return_adjusted=pd.to_numeric(returns["return_adjusted"], errors="coerce"))
        .pivot(index="trade_date", columns="ts_code", values="return_adjusted")
        .sort_index()
    )
    trade_dates = return_matrix.index.to_list()
    held_codes = sorted(weights["ts_code"].dropna().astype(str).unique())
    suspension_matrix = build_suspension_matrix(suspensions, trade_dates, held_codes)
    daily_rows: list[dict[str, object]] = []
    turnover_rows: list[dict[str, object]] = []

    for factor, factor_weights in weights.groupby("factor"):
        previous: pd.Series | None = None
        periods = rebalance_calendar.sort_values("signal_date").reset_index(drop=True)
        for idx, period in periods.iterrows():
            signal_date = str(period["signal_date"])
            exec_date = str(period["exec_date"])
            current_rows = factor_weights[factor_weights["trade_date"] == signal_date]
            if current_rows.empty:
                continue
            current = current_rows.set_index("ts_code")["weight"].astype(float)
            turnover = one_way_turnover(previous, current)
            turnover_rows.append(
                {
                    "factor": factor,
                    "signal_date": signal_date,
                    "exec_date": exec_date,
                    "turnover": turnover,
                    "holding_count": int(current.shape[0]),
                }
            )

            next_exec = None
            if idx + 1 < len(periods):
                next_exec = str(periods.loc[idx + 1, "exec_date"])
            period_dates = [d for d in trade_dates if d > exec_date and d <= (next_exec or end_date)]
            if period_dates:
                available = return_matrix.reindex(index=period_dates, columns=current.index)
                suspended = suspension_matrix.reindex(index=period_dates, columns=current.index, fill_value=False).astype(bool)
                valid_returns = available.notna()
                tradable_or_suspended = valid_returns | suspended
                invalid_missing = ~tradable_or_suspended
                missing_counts = available.isna().sum(axis=1)
                suspended_counts = (available.isna() & suspended).sum(axis=1)
                invalid_missing_counts = invalid_missing.sum(axis=1)
                weight_frame = pd.DataFrame(1.0, index=period_dates, columns=current.index).mul(current, axis=1)
                active_weights = weight_frame.where(tradable_or_suspended, 0.0)
                active_weight_sum = active_weights.sum(axis=1)
                weighted_returns = available.fillna(0.0).mul(weight_frame, axis=0).where(tradable_or_suspended, 0.0)
                portfolio_returns = weighted_returns.sum(axis=1).div(active_weight_sum).where(active_weight_sum > 0, 0.0)
                invalid_missing_weights = weight_frame.where(invalid_missing, 0.0).sum(axis=1)
                first_date = portfolio_returns.index[0]
                portfolio_returns.loc[first_date] = portfolio_returns.loc[first_date] - turnover * transaction_cost
                for date, ret in portfolio_returns.items():
                    daily_rows.append(
                        {
                            "factor": factor,
                            "trade_date": date,
                            "portfolio_return": float(ret),
                            "turnover": float(turnover if date == first_date else 0.0),
                            "missing_return_count": int(missing_counts.loc[date]),
                            "suspended_return_count": int(suspended_counts.loc[date]),
                            "invalid_missing_return_count": int(invalid_missing_counts.loc[date]),
                            "invalid_missing_weight": float(invalid_missing_weights.loc[date]),
                        }
                    )
            previous = current

    daily = pd.DataFrame(daily_rows)
    turnover = pd.DataFrame(turnover_rows)
    if not daily.empty:
        daily = daily.sort_values(["factor", "trade_date"]).reset_index(drop=True)
    return daily, turnover


def compute_performance_metrics(
    portfolio_returns: pd.DataFrame,
    benchmark_returns: pd.DataFrame,
    turnover: pd.DataFrame,
) -> pd.DataFrame:
    if portfolio_returns.empty:
        return pd.DataFrame()
    benchmark = benchmark_returns.set_index("trade_date")["benchmark_return"].astype(float)
    rows = []
    for factor, group in portfolio_returns.groupby("factor"):
        series = group.set_index("trade_date")["portfolio_return"].astype(float).sort_index()
        nav = (1.0 + series).cumprod()
        n = len(series)
        annual_return = nav.iloc[-1] ** (252 / n) - 1 if n else 0.0
        annual_volatility = series.std(ddof=0) * math.sqrt(252) if n else 0.0
        sharpe = annual_return / annual_volatility if annual_volatility else pd.NA
        drawdown = nav / nav.cummax() - 1.0
        bench_aligned = benchmark.reindex(series.index).fillna(0.0)
        benchmark_nav = (1.0 + bench_aligned).cumprod()
        benchmark_annual = benchmark_nav.iloc[-1] ** (252 / n) - 1 if n else 0.0
        factor_turnover = turnover[turnover["factor"] == factor]["turnover"]
        rows.append(
            {
                "factor": factor,
                "start_date": series.index.min(),
                "end_date": series.index.max(),
                "trading_days": n,
                "annual_return": annual_return,
                "annual_volatility": annual_volatility,
                "max_drawdown": drawdown.min(),
                "sharpe": sharpe,
                "cumulative_return": nav.iloc[-1] - 1.0,
                "benchmark_annual_return": benchmark_annual,
                "excess_annual_return": annual_return - benchmark_annual,
                "average_monthly_turnover": factor_turnover.mean() if not factor_turnover.empty else pd.NA,
                "annualized_turnover": factor_turnover.mean() * 12 if not factor_turnover.empty else pd.NA,
                "missing_return_count": int(group["missing_return_count"].sum()),
                "suspended_return_count": int(group["suspended_return_count"].sum()) if "suspended_return_count" in group else 0,
                "invalid_missing_return_count": int(group["invalid_missing_return_count"].sum()) if "invalid_missing_return_count" in group else 0,
                "average_invalid_missing_weight": (
                    group["invalid_missing_weight"].mean() if "invalid_missing_weight" in group else 0.0
                ),
                "max_invalid_missing_weight": (
                    group["invalid_missing_weight"].max() if "invalid_missing_weight" in group else 0.0
                ),
            }
        )
    return pd.DataFrame(rows).sort_values("annual_return", ascending=False).reset_index(drop=True)


def factor_coverage(scores: pd.DataFrame, weights: pd.DataFrame, score_col: str = "score") -> pd.DataFrame:
    if scores.empty:
        return pd.DataFrame()
    by_date = (
        scores.assign(is_valid=scores[score_col].notna())
        .groupby(["factor", "trade_date"], as_index=False)
        .agg(valid_count=("is_valid", "sum"))
    )
    coverage = (
        by_date.groupby("factor", as_index=False)
        .agg(
            signal_count=("trade_date", "nunique"),
            first_signal=("trade_date", "min"),
            last_signal=("trade_date", "max"),
            average_valid_stocks=("valid_count", "mean"),
            valid_rows=("valid_count", "sum"),
        )
    )
    if not weights.empty:
        holdings = (
            weights.groupby(["factor", "trade_date"], as_index=False)
            .agg(holding_count=("ts_code", "count"))
            .groupby("factor", as_index=False)
            .agg(average_holding_count=("holding_count", "mean"))
        )
        coverage = coverage.merge(holdings, on="factor", how="left")
    return coverage


def compare_factor_metrics(raw_metrics: pd.DataFrame, neutralized_metrics: pd.DataFrame) -> pd.DataFrame:
    if raw_metrics.empty and neutralized_metrics.empty:
        return pd.DataFrame()
    metric_cols = [
        "annual_return",
        "excess_annual_return",
        "sharpe",
        "max_drawdown",
        "cumulative_return",
        "average_monthly_turnover",
    ]
    raw = raw_metrics[["factor", *[col for col in metric_cols if col in raw_metrics.columns]]].copy()
    neutralized = neutralized_metrics[["factor", *[col for col in metric_cols if col in neutralized_metrics.columns]]].copy()
    merged = raw.merge(neutralized, on="factor", how="outer", suffixes=("_raw", "_neutralized"))
    for col in metric_cols:
        raw_col = f"{col}_raw"
        neutralized_col = f"{col}_neutralized"
        if raw_col in merged.columns and neutralized_col in merged.columns:
            merged[f"{col}_delta"] = merged[neutralized_col] - merged[raw_col]
    ordered = ["factor"]
    for col in metric_cols:
        ordered.extend(
            value
            for value in (f"{col}_raw", f"{col}_neutralized", f"{col}_delta")
            if value in merged.columns
        )
    sort_col = "annual_return_delta" if "annual_return_delta" in merged.columns else "factor"
    return merged[ordered].sort_values(sort_col, ascending=False, na_position="last").reset_index(drop=True)


class FactorResearchRunner:
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
        neutralized_subdir: str = "neutralized",
    ) -> None:
        self.project_root = Path(project_root)
        self.db_path = Path(db_path)
        self.output_dir = Path(output_dir)
        self.start = yyyymmdd(start)
        self.end = yyyymmdd(end) if end else None
        self.warmup_start = yyyymmdd(warmup_start)
        self.benchmark = benchmark
        self.transaction_cost = transaction_cost
        neutralized_path = Path(neutralized_subdir)
        if not neutralized_subdir or neutralized_path.is_absolute() or ".." in neutralized_path.parts:
            raise ValueError("neutralized_subdir must be a relative subdirectory under output_dir")
        self.neutralized_subdir = neutralized_subdir

    def run(self) -> dict[str, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "tables").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "images").mkdir(parents=True, exist_ok=True)

        with duckdb.connect(str(self.db_path), read_only=True) as con:
            end_date = self.end or self._latest_trade_date(con)
            all_trade_dates = self._load_trade_dates(con, self.warmup_start, end_date)
            rebalance_calendar = build_rebalance_calendar(all_trade_dates, self.start, end_date)
            signal_dates = rebalance_calendar["signal_date"].drop_duplicates().reset_index(drop=True)
            con.register("signal_dates", pd.DataFrame({"trade_date": signal_dates}))

            raw_factors = self._load_factor_panel(con, signal_dates, end_date)
            scores = normalize_factor_panel(raw_factors)
            execution_universe = self._load_execution_universe(con, rebalance_calendar)
            tradable_scores = filter_scores_for_execution_universe(scores, rebalance_calendar, execution_universe)
            weights = select_top_quantile_weights(tradable_scores, quantile=0.2)
            returns = self._load_returns(con, self.start, end_date)
            suspensions = self._load_suspensions(con, self.start, end_date)
            benchmark_returns = self._load_benchmark(con, self.warmup_start, end_date)
            exposures = self._load_exposures(con, signal_dates)

        portfolio_returns, turnover = backtest_monthly_top_quantile(
            weights,
            returns,
            rebalance_calendar,
            end_date=end_date,
            transaction_cost=self.transaction_cost,
            suspensions=suspensions,
        )
        metrics = compute_performance_metrics(portfolio_returns, benchmark_returns, turnover)
        coverage = factor_coverage(scores, weights)
        nav = self._build_nav_table(portfolio_returns, benchmark_returns)
        monthly_returns = self._build_monthly_returns(portfolio_returns)

        neutralized_scores = neutralize_scores(scores, exposures)
        tradable_neutralized_scores = filter_scores_for_execution_universe(
            neutralized_scores,
            rebalance_calendar,
            execution_universe,
        )
        neutralized_weights = select_top_quantile_weights(
            tradable_neutralized_scores,
            quantile=0.2,
            score_col="neutralized_score",
        )
        neutralized_portfolio_returns, neutralized_turnover = backtest_monthly_top_quantile(
            neutralized_weights,
            returns,
            rebalance_calendar,
            end_date=end_date,
            transaction_cost=self.transaction_cost,
            suspensions=suspensions,
        )
        neutralized_metrics = compute_performance_metrics(
            neutralized_portfolio_returns,
            benchmark_returns,
            neutralized_turnover,
        )
        neutralized_coverage = factor_coverage(
            neutralized_scores,
            neutralized_weights,
            score_col="neutralized_score",
        )
        neutralized_nav = self._build_nav_table(neutralized_portfolio_returns, benchmark_returns)
        neutralized_monthly_returns = self._build_monthly_returns(neutralized_portfolio_returns)
        metrics_comparison = compare_factor_metrics(metrics, neutralized_metrics)

        paths = self._write_outputs(
            metrics=metrics,
            coverage=coverage,
            nav=nav,
            monthly_returns=monthly_returns,
            scores=scores,
            weights=weights,
            turnover=turnover,
            benchmark_returns=benchmark_returns,
            end_date=end_date,
        )
        paths.update(
            self._write_neutralized_outputs(
                metrics=neutralized_metrics,
                coverage=neutralized_coverage,
                nav=neutralized_nav,
                monthly_returns=neutralized_monthly_returns,
                weights=neutralized_weights,
                turnover=neutralized_turnover,
                metrics_comparison=metrics_comparison,
                raw_metrics=metrics,
                benchmark_returns=benchmark_returns,
                end_date=end_date,
            )
        )
        return paths

    def _latest_trade_date(self, con: duckdb.DuckDBPyConnection) -> str:
        value = con.execute("select max(trade_date) from v_adjusted_returns").fetchone()[0]
        if not value:
            raise RuntimeError("v_adjusted_returns has no trade dates")
        return str(value)

    def _load_trade_dates(self, con: duckdb.DuckDBPyConnection, start: str, end: str) -> pd.Series:
        frame = con.execute(
            """
            select distinct trade_date
            from v_adjusted_returns
            where trade_date between ? and ?
            order by trade_date
            """,
            [start, end],
        ).fetchdf()
        return frame["trade_date"]

    def _load_execution_universe(self, con: duckdb.DuckDBPyConnection, rebalance_calendar: pd.DataFrame) -> pd.DataFrame:
        if rebalance_calendar.empty:
            return pd.DataFrame(columns=["exec_date", "ts_code"])
        exec_dates = rebalance_calendar[["exec_date"]].dropna().drop_duplicates()
        con.register("exec_dates", exec_dates)
        return con.execute(
            """
            select
                u.trade_date as exec_date,
                u.ts_code
            from v_stock_universe_daily u
            join exec_dates e
              on u.trade_date = e.exec_date
            where u.is_listed_on_date = true
              and u.is_suspended = false
              and u.is_st_name = false
            """
        ).fetchdf()

    def _load_exposures(self, con: duckdb.DuckDBPyConnection, signal_dates: pd.Series) -> pd.DataFrame:
        if signal_dates.empty:
            return pd.DataFrame(columns=["trade_date", "ts_code", "total_mv", "log_total_mv", "industry"])
        con.register("factor_signal_dates", pd.DataFrame({"trade_date": signal_dates}))
        daily_basic = self._silver_path("daily_basic")
        size = con.execute(
            f"""
            select
                db.trade_date,
                db.ts_code,
                db.total_mv,
                case when db.total_mv > 0 then ln(db.total_mv) else null end as log_total_mv
            from read_parquet('{daily_basic}') db
            join factor_signal_dates s using (trade_date)
            """
        ).fetchdf()
        industry = con.execute(
            """
            with candidates as (
                select
                    s.trade_date,
                    i.ts_code,
                    i.l1_name as industry,
                    row_number() over (
                        partition by s.trade_date, i.ts_code
                        order by i.in_date desc
                    ) as rn
                from factor_signal_dates s
                join v_industry_data i
                  on i.ts_code is not null
                 and cast(i.in_date as varchar) <= s.trade_date
                 and (i.out_date is null or s.trade_date < cast(i.out_date as varchar))
            )
            select trade_date, ts_code, industry
            from candidates
            where rn = 1
            """
        ).fetchdf()
        exposures = size.merge(industry, on=["trade_date", "ts_code"], how="left")
        exposures["industry"] = exposures["industry"].fillna("Unknown")
        return exposures

    def _load_factor_panel(self, con: duckdb.DuckDBPyConnection, signal_dates: pd.Series, end_date: str) -> pd.DataFrame:
        if signal_dates.empty:
            return pd.DataFrame(columns=["factor", "trade_date", "ts_code", "raw_value"])
        queries = [
            self._financial_factor_query("ROE", "roe"),
            self._daily_basic_factor_query("PE_TTM", "pe_ttm", "pe_ttm > 0"),
            self._financial_factor_query("Revenue_Growth", "tr_yoy"),
            self._momentum_query(),
            self._turnover_query(),
            self._financial_factor_query("Debt_to_Equity", "debt_to_eqt"),
            self._volatility_query(),
            self._moneyflow_query(),
            self._daily_basic_factor_query("Dividend_Yield", "dv_ttm", "dv_ttm is not null"),
            self._holder_concentration_query(),
            self._financial_factor_query("Gross_Margin", "grossprofit_margin"),
        ]
        frames = [con.execute(query, [self.warmup_start, end_date]).fetchdf() for query in queries]
        panel = pd.concat(frames, ignore_index=True)
        panel["raw_value"] = pd.to_numeric(panel["raw_value"], errors="coerce")
        panel = panel.dropna(subset=["raw_value"])
        return (
            panel.groupby(["factor", "trade_date", "ts_code"], as_index=False)
            .agg(raw_value=("raw_value", "mean"))
        )

    def _universe_join(self, alias: str = "s") -> str:
        return f"""
        join v_stock_universe_daily u
          on u.ts_code = {alias}.ts_code
         and u.trade_date = {alias}.trade_date
         and u.is_listed_on_date = true
         and u.is_suspended = false
         and u.is_st_name = false
        """

    def _financial_factor_query(self, factor: str, column: str) -> str:
        return f"""
        with candidates as (
            select
                '{factor}' as factor,
                s.trade_date,
                f.ts_code,
                f.{column} as raw_value,
                row_number() over (
                    partition by s.trade_date, f.ts_code
                    order by f.end_date desc, f.visible_from desc, f.ann_date desc
                ) as rn
            from signal_dates s
            join v_fina_indicator_asof_intervals f
              on s.trade_date >= f.visible_from
             and (f.next_visible_from is null or s.trade_date < f.next_visible_from)
            join v_stock_universe_daily u
              on u.ts_code = f.ts_code
             and u.trade_date = s.trade_date
             and u.is_listed_on_date = true
             and u.is_suspended = false
             and u.is_st_name = false
            where s.trade_date between ? and ?
              and f.visible_from is not null
              and f.{column} is not null
        )
        select
            factor,
            trade_date,
            ts_code,
            raw_value
        from candidates
        where rn = 1
        """

    def _daily_basic_factor_query(self, factor: str, column: str, extra_filter: str) -> str:
        path = self._silver_path("daily_basic")
        return f"""
        select
            '{factor}' as factor,
            db.trade_date,
            db.ts_code,
            db.{column} as raw_value
        from read_parquet('{path}') db
        join signal_dates s using (trade_date)
        {self._universe_join("db")}
        where db.trade_date between ? and ?
          and {extra_filter}
        """

    def _momentum_query(self) -> str:
        return f"""
        with base as (
            select
                ts_code,
                trade_date,
                close_hfq,
                lag(close_hfq, 60) over (partition by ts_code order by trade_date) as close_hfq_lag60
            from v_adjusted_returns
            where trade_date between ? and ?
        )
        select
            'Momentum_60D' as factor,
            b.trade_date,
            b.ts_code,
            b.close_hfq / nullif(b.close_hfq_lag60, 0) - 1 as raw_value
        from base b
        join signal_dates s using (trade_date)
        {self._universe_join("b")}
        where b.close_hfq_lag60 is not null
        """

    def _turnover_query(self) -> str:
        path = self._silver_path("daily_basic")
        return f"""
        with base as (
            select
                ts_code,
                trade_date,
                avg(turnover_rate_f) over (
                    partition by ts_code
                    order by trade_date
                    rows between 19 preceding and current row
                ) as turnover_20d
                ,
                count(turnover_rate_f) over (
                    partition by ts_code
                    order by trade_date
                    rows between 19 preceding and current row
                ) as turnover_obs
            from read_parquet('{path}')
            where trade_date between ? and ?
        )
        select
            'Turnover_20D' as factor,
            b.trade_date,
            b.ts_code,
            b.turnover_20d as raw_value
        from base b
        join signal_dates s using (trade_date)
        {self._universe_join("b")}
        where b.turnover_20d is not null
          and b.turnover_obs = 20
        """

    def _volatility_query(self) -> str:
        return f"""
        with base as (
            select
                ts_code,
                trade_date,
                stddev_samp(return_adjusted) over (
                    partition by ts_code
                    order by trade_date
                    rows between 251 preceding and current row
                ) * sqrt(252.0) as volatility,
                count(return_adjusted) over (
                    partition by ts_code
                    order by trade_date
                    rows between 251 preceding and current row
                ) as return_obs
            from v_adjusted_returns
            where trade_date between ? and ?
        )
        select
            'Volatility' as factor,
            b.trade_date,
            b.ts_code,
            b.volatility as raw_value
        from base b
        join signal_dates s using (trade_date)
        {self._universe_join("b")}
        where b.volatility is not null
          and b.return_obs = 252
        """

    def _moneyflow_query(self) -> str:
        path = self._silver_path("moneyflow")
        return f"""
        select
            'Main_Net_In' as factor,
            m.trade_date,
            m.ts_code,
            coalesce(m.buy_lg_amount, 0) + coalesce(m.buy_elg_amount, 0)
              - coalesce(m.sell_lg_amount, 0) - coalesce(m.sell_elg_amount, 0) as raw_value
        from read_parquet('{path}') m
        join signal_dates s using (trade_date)
        {self._universe_join("m")}
        where m.trade_date between ? and ?
        """

    def _holder_concentration_query(self) -> str:
        path = self._silver_path("top10_holders")
        return f"""
        with grouped as (
            select
                ts_code,
                ann_date as visible_from,
                end_date,
                sum(hold_ratio) as holder_concen
            from read_parquet('{path}')
            where ann_date is not null
            group by ts_code, ann_date, end_date
        ),
        latest_per_announcement as (
            select * exclude (rn)
            from (
                select
                    *,
                    row_number() over (
                        partition by ts_code, visible_from
                        order by end_date desc
                    ) as rn
                from grouped
            )
            where rn = 1
        ),
        intervals as (
            select
                *,
                lead(visible_from) over (
                    partition by ts_code
                    order by visible_from, end_date
                ) as next_visible_from
            from latest_per_announcement
        )
        select
            'Holder_Concen' as factor,
            s.trade_date,
            h.ts_code,
            h.holder_concen as raw_value
        from signal_dates s
        join intervals h
          on s.trade_date >= h.visible_from
         and (h.next_visible_from is null or s.trade_date < h.next_visible_from)
        join v_stock_universe_daily u
          on u.ts_code = h.ts_code
         and u.trade_date = s.trade_date
         and u.is_listed_on_date = true
         and u.is_suspended = false
         and u.is_st_name = false
        where s.trade_date between ? and ?
          and h.holder_concen is not null
        """

    def _load_returns(self, con: duckdb.DuckDBPyConnection, start: str, end: str) -> pd.DataFrame:
        return con.execute(
            """
            select
                ts_code,
                trade_date,
                return_adjusted
            from v_adjusted_returns
            where trade_date between ? and ?
            """,
            [start, end],
        ).fetchdf()

    def _load_suspensions(self, con: duckdb.DuckDBPyConnection, start: str, end: str) -> pd.DataFrame:
        path_obj = self.project_root / "data" / "silver" / "suspend_d.parquet"
        if not path_obj.exists():
            return pd.DataFrame(columns=["ts_code", "trade_date"])
        path = self._silver_path("suspend_d")
        return con.execute(
            f"""
            select distinct
                ts_code,
                trade_date
            from read_parquet('{path}')
            where trade_date between ? and ?
            """,
            [start, end],
        ).fetchdf()

    def _load_benchmark(self, con: duckdb.DuckDBPyConnection, start: str, end: str) -> pd.DataFrame:
        return con.execute(
            """
            with index_close as (
                select trade_date, close
                from v_index_data
                where ts_code = ?
                  and trade_date between ? and ?
                  and close is not null
            )
            select
                trade_date,
                close / nullif(lag(close) over (order by trade_date), 0) - 1 as benchmark_return
            from index_close
            order by trade_date
            """,
            [self.benchmark, start, end],
        ).fetchdf()

    def _silver_path(self, dataset: str) -> str:
        return str((self.project_root / "data" / "silver" / f"{dataset}.parquet").resolve()).replace("\\", "/").replace("'", "''")

    def _build_nav_table(self, portfolio_returns: pd.DataFrame, benchmark_returns: pd.DataFrame) -> pd.DataFrame:
        if portfolio_returns.empty:
            return pd.DataFrame()
        frames = []
        benchmark = benchmark_returns.copy()
        first_portfolio_date = portfolio_returns["trade_date"].min()
        benchmark = benchmark[benchmark["trade_date"] >= first_portfolio_date]
        benchmark["factor"] = "Benchmark_000985_CSI"
        benchmark["nav"] = (1.0 + benchmark["benchmark_return"].fillna(0.0)).cumprod()
        benchmark = benchmark.rename(columns={"benchmark_return": "daily_return"})
        frames.append(benchmark[["factor", "trade_date", "daily_return", "nav"]])
        for factor, group in portfolio_returns.groupby("factor"):
            temp = group[["trade_date", "portfolio_return"]].copy()
            temp["factor"] = factor
            temp["daily_return"] = temp["portfolio_return"]
            temp["nav"] = (1.0 + temp["daily_return"].fillna(0.0)).cumprod()
            frames.append(temp[["factor", "trade_date", "daily_return", "nav"]])
        return pd.concat(frames, ignore_index=True)

    def _build_monthly_returns(self, portfolio_returns: pd.DataFrame) -> pd.DataFrame:
        if portfolio_returns.empty:
            return pd.DataFrame()
        frame = portfolio_returns.copy()
        frame["month"] = pd.to_datetime(frame["trade_date"], format=DATE_FMT).dt.to_period("M").astype(str)
        return (
            frame.groupby(["factor", "month"])["portfolio_return"]
            .apply(lambda s: (1.0 + s).prod() - 1.0)
            .reset_index(name="monthly_return")
        )

    def _write_outputs(
        self,
        metrics: pd.DataFrame,
        coverage: pd.DataFrame,
        nav: pd.DataFrame,
        monthly_returns: pd.DataFrame,
        scores: pd.DataFrame,
        weights: pd.DataFrame,
        turnover: pd.DataFrame,
        benchmark_returns: pd.DataFrame,
        end_date: str,
    ) -> dict[str, Path]:
        tables = self.output_dir / "tables"
        images = self.output_dir / "images"
        paths = {
            "metrics": tables / "factor_metrics.csv",
            "coverage": tables / "factor_coverage.csv",
            "nav": tables / "nav_by_factor.csv",
            "monthly_returns": tables / "monthly_returns.csv",
            "turnover": tables / "turnover.csv",
            "weights": tables / "rebalance_weights.csv",
            "report": self.output_dir / "report.md",
        }
        metrics.to_csv(paths["metrics"], index=False, encoding="utf-8-sig")
        coverage.to_csv(paths["coverage"], index=False, encoding="utf-8-sig")
        nav.to_csv(paths["nav"], index=False, encoding="utf-8-sig")
        monthly_returns.to_csv(paths["monthly_returns"], index=False, encoding="utf-8-sig")
        turnover.to_csv(paths["turnover"], index=False, encoding="utf-8-sig")
        weights.to_csv(paths["weights"], index=False, encoding="utf-8-sig")

        chart_paths = self._write_images(metrics, coverage, nav, images)
        paths.update(chart_paths)
        self._write_report(paths["report"], metrics, coverage, end_date, benchmark_returns)
        return paths

    def _write_neutralized_outputs(
        self,
        metrics: pd.DataFrame,
        coverage: pd.DataFrame,
        nav: pd.DataFrame,
        monthly_returns: pd.DataFrame,
        weights: pd.DataFrame,
        turnover: pd.DataFrame,
        metrics_comparison: pd.DataFrame,
        raw_metrics: pd.DataFrame,
        benchmark_returns: pd.DataFrame,
        end_date: str,
    ) -> dict[str, Path]:
        base = self.output_dir / self.neutralized_subdir
        tables = base / "tables"
        images = base / "images"
        tables.mkdir(parents=True, exist_ok=True)
        images.mkdir(parents=True, exist_ok=True)
        paths = {
            "neutralized_metrics": tables / "factor_metrics.csv",
            "neutralized_coverage": tables / "factor_coverage.csv",
            "neutralized_nav": tables / "nav_by_factor.csv",
            "neutralized_monthly_returns": tables / "monthly_returns.csv",
            "neutralized_turnover": tables / "turnover.csv",
            "neutralized_weights": tables / "rebalance_weights.csv",
            "neutralized_metrics_comparison": tables / "factor_metrics_comparison.csv",
            "neutralized_report": base / "report.md",
        }
        metrics.to_csv(paths["neutralized_metrics"], index=False, encoding="utf-8-sig")
        coverage.to_csv(paths["neutralized_coverage"], index=False, encoding="utf-8-sig")
        nav.to_csv(paths["neutralized_nav"], index=False, encoding="utf-8-sig")
        monthly_returns.to_csv(paths["neutralized_monthly_returns"], index=False, encoding="utf-8-sig")
        turnover.to_csv(paths["neutralized_turnover"], index=False, encoding="utf-8-sig")
        weights.to_csv(paths["neutralized_weights"], index=False, encoding="utf-8-sig")
        metrics_comparison.to_csv(paths["neutralized_metrics_comparison"], index=False, encoding="utf-8-sig")

        chart_paths = {
            f"neutralized_{key}": path
            for key, path in self._write_images(metrics, coverage, nav, images).items()
        }
        chart_paths.update(
            {
                f"neutralized_{key}": path
                for key, path in self._write_comparison_images(metrics_comparison, images).items()
            }
        )
        paths.update(chart_paths)
        self._write_neutralized_report(
            paths["neutralized_report"],
            metrics=metrics,
            coverage=coverage,
            metrics_comparison=metrics_comparison,
            raw_metrics=raw_metrics,
            end_date=end_date,
            benchmark_returns=benchmark_returns,
            image_paths=chart_paths,
        )
        return paths

    def _write_images(self, metrics: pd.DataFrame, coverage: pd.DataFrame, nav: pd.DataFrame, images: Path) -> dict[str, Path]:
        try:
            import matplotlib.pyplot as plt
            from matplotlib.ticker import PercentFormatter
        except ImportError:
            return {}

        plt.style.use("seaborn-v0_8-whitegrid")
        paths: dict[str, Path] = {}
        if not nav.empty:
            colors = self._plot_colors(nav["factor"].unique())
            nav_path = images / "nav_curve.png"
            fig, ax = plt.subplots(figsize=(14, 8))
            for factor, group in nav.groupby("factor"):
                dates = pd.to_datetime(group["trade_date"], format=DATE_FMT)
                is_benchmark = factor == "Benchmark_000985_CSI"
                width = 2.8 if is_benchmark else 1.6
                alpha = 0.95 if is_benchmark else 0.82
                ax.plot(
                    dates,
                    group["nav"],
                    label=FACTOR_DISPLAY_NAMES.get(factor, factor),
                    linewidth=width,
                    alpha=alpha,
                    color=colors[factor],
                )
            ax.set_title("Single Factor Top20% NAV", fontsize=16)
            ax.set_xlabel("Date")
            ax.set_ylabel("NAV")
            ax.grid(True, alpha=0.25)
            ax.legend(
                fontsize=8.5,
                ncol=4,
                loc="upper center",
                bbox_to_anchor=(0.5, -0.12),
                frameon=False,
            )
            fig.tight_layout(rect=(0, 0.06, 1, 1))
            fig.savefig(nav_path, dpi=170)
            plt.close(fig)
            paths["nav_image"] = nav_path

            drawdown_path = images / "drawdown.png"
            fig, ax = plt.subplots(figsize=(14, 8))
            for factor, group in nav.groupby("factor"):
                dd = group["nav"] / group["nav"].cummax() - 1.0
                dates = pd.to_datetime(group["trade_date"], format=DATE_FMT)
                is_benchmark = factor == "Benchmark_000985_CSI"
                width = 2.5 if is_benchmark else 1.4
                ax.plot(
                    dates,
                    dd,
                    label=FACTOR_DISPLAY_NAMES.get(factor, factor),
                    linewidth=width,
                    alpha=0.82,
                    color=colors[factor],
                )
            ax.set_title("Drawdown", fontsize=16)
            ax.set_xlabel("Date")
            ax.set_ylabel("Drawdown")
            ax.yaxis.set_major_formatter(PercentFormatter(1.0))
            ax.grid(True, alpha=0.25)
            ax.legend(
                fontsize=8.5,
                ncol=4,
                loc="upper center",
                bbox_to_anchor=(0.5, -0.12),
                frameon=False,
            )
            fig.tight_layout(rect=(0, 0.06, 1, 1))
            fig.savefig(drawdown_path, dpi=170)
            plt.close(fig)
            paths["drawdown_image"] = drawdown_path

        if not metrics.empty:
            metrics_path = images / "metrics_table.png"
            cols = ["factor", "annual_return", "annual_volatility", "max_drawdown", "sharpe", "excess_annual_return"]
            table = metrics[cols].copy()
            table["factor"] = table["factor"].map(lambda value: FACTOR_DISPLAY_NAMES.get(value, value))
            for col in cols[1:]:
                table[col] = table[col].map(lambda x: "" if pd.isna(x) else f"{x:.2%}" if col != "sharpe" else f"{x:.2f}")
            table = table.rename(
                columns={
                    "factor": "Factor",
                    "annual_return": "Ann. Return",
                    "annual_volatility": "Ann. Vol.",
                    "max_drawdown": "Max DD",
                    "sharpe": "Sharpe",
                    "excess_annual_return": "Excess Ann.",
                }
            )
            self._save_table_image(table, metrics_path, "Performance Metrics")
            paths["metrics_image"] = metrics_path

        if not coverage.empty:
            coverage_path = images / "coverage_table.png"
            table = coverage[["factor", "signal_count", "first_signal", "last_signal", "valid_rows", "average_holding_count"]].copy()
            table["factor"] = table["factor"].map(lambda value: FACTOR_DISPLAY_NAMES.get(value, value))
            table["first_signal"] = table["first_signal"].map(_format_yyyymmdd)
            table["last_signal"] = table["last_signal"].map(_format_yyyymmdd)
            table["average_holding_count"] = table["average_holding_count"].map(lambda x: "" if pd.isna(x) else f"{x:.0f}")
            table = table.rename(
                columns={
                    "factor": "Factor",
                    "signal_count": "Signals",
                    "first_signal": "First Signal",
                    "last_signal": "Last Signal",
                    "valid_rows": "Valid Rows",
                    "average_holding_count": "Avg Holdings",
                }
            )
            self._save_table_image(table, coverage_path, "Factor Coverage")
            paths["coverage_image"] = coverage_path
        return paths

    def _write_comparison_images(self, metrics_comparison: pd.DataFrame, images: Path) -> dict[str, Path]:
        try:
            import matplotlib.pyplot as plt
            from matplotlib.ticker import PercentFormatter
        except ImportError:
            return {}
        paths: dict[str, Path] = {}
        if metrics_comparison.empty:
            return paths

        table_path = images / "metrics_comparison_table.png"
        cols = [
            "factor",
            "annual_return_raw",
            "annual_return_neutralized",
            "annual_return_delta",
            "excess_annual_return_delta",
            "sharpe_delta",
            "max_drawdown_delta",
        ]
        table = metrics_comparison[[col for col in cols if col in metrics_comparison.columns]].copy()
        table["factor"] = table["factor"].map(lambda value: FACTOR_DISPLAY_NAMES.get(value, value))
        for col in table.columns:
            if col == "factor":
                continue
            table[col] = table[col].map(lambda x: "" if pd.isna(x) else f"{x:.2f}" if "sharpe" in col else f"{x:.2%}")
        table = table.rename(
            columns={
                "factor": "Factor",
                "annual_return_raw": "Raw Ann.",
                "annual_return_neutralized": "Neutral Ann.",
                "annual_return_delta": "Delta Ann.",
                "excess_annual_return_delta": "Delta Excess",
                "sharpe_delta": "Delta Sharpe",
                "max_drawdown_delta": "Delta Max DD",
            }
        )
        self._save_table_image(table, table_path, "Raw vs Neutralized Metrics")
        paths["metrics_comparison_image"] = table_path

        required = {"factor", "annual_return_raw", "annual_return_neutralized"}
        if required <= set(metrics_comparison.columns):
            annual_path = images / "raw_vs_neutralized_annual_return.png"
            plot_frame = metrics_comparison.sort_values("annual_return_neutralized", ascending=True)
            y = np.arange(len(plot_frame))
            height = max(4.5, len(plot_frame) * 0.42)
            fig, ax = plt.subplots(figsize=(10.5, height))
            ax.barh(y - 0.18, plot_frame["annual_return_raw"], height=0.36, label="Raw", color="#4c78a8")
            ax.barh(y + 0.18, plot_frame["annual_return_neutralized"], height=0.36, label="Neutralized", color="#f58518")
            ax.axvline(0, color="#333333", linewidth=0.8)
            ax.set_yticks(y, [FACTOR_DISPLAY_NAMES.get(value, value) for value in plot_frame["factor"]])
            ax.set_xlabel("Annual Return")
            ax.set_title("Raw vs Neutralized Annual Return")
            ax.xaxis.set_major_formatter(PercentFormatter(1.0))
            ax.legend(frameon=False)
            fig.tight_layout()
            fig.savefig(annual_path, dpi=170)
            plt.close(fig)
            paths["annual_return_comparison_image"] = annual_path
        return paths

    def _plot_colors(self, factors: Iterable[str]) -> dict[str, object]:
        import matplotlib.pyplot as plt

        ordered = sorted(factors, key=lambda item: (item != "Benchmark_000985_CSI", item))
        palette = list(plt.get_cmap("tab20").colors)
        colors: dict[str, object] = {}
        for idx, factor in enumerate(ordered):
            colors[factor] = "#111111" if factor == "Benchmark_000985_CSI" else palette[(idx - 1) % len(palette)]
        return colors

    def _save_table_image(self, table: pd.DataFrame, path: Path, title: str) -> None:
        import matplotlib.pyplot as plt

        height = max(3.6, 0.36 * (len(table) + 2))
        fig, ax = plt.subplots(figsize=(13.5, height))
        ax.axis("off")
        ax.set_title(title, fontsize=15, pad=8, weight="bold")
        rendered = ax.table(
            cellText=table.values,
            colLabels=table.columns,
            loc="center",
            cellLoc="center",
            bbox=[0.0, 0.0, 1.0, 0.88],
        )
        rendered.auto_set_font_size(False)
        rendered.set_fontsize(9)
        for (row, _col), cell in rendered.get_celld().items():
            cell.set_edgecolor("#d0d0d0")
            cell.set_linewidth(0.6)
            if row == 0:
                cell.set_text_props(weight="bold", color="white")
                cell.set_facecolor("#333333")
            elif row % 2 == 0:
                cell.set_facecolor("#f5f5f5")
        fig.tight_layout()
        fig.savefig(path, dpi=170)
        plt.close(fig)

    def _write_report(
        self,
        path: Path,
        metrics: pd.DataFrame,
        coverage: pd.DataFrame,
        end_date: str,
        benchmark_returns: pd.DataFrame,
    ) -> None:
        factor_table = pd.DataFrame(
            [
                {
                    "因子": spec.name,
                    "类别": spec.category,
                    "中文名称": spec.chinese_name,
                    "方向": "越高越好" if spec.direction > 0 else "越低越好",
                    "公式": spec.formula,
                }
                for spec in FACTOR_SPECS
            ]
        )
        top_metrics = metrics.head(11).copy()
        metric_lines = []
        for row in top_metrics.to_dict("records"):
            sharpe = "" if pd.isna(row["sharpe"]) else f"{row['sharpe']:.2f}"
            metric_lines.append(
                f"| {row['factor']} | {row['annual_return']:.2%} | {row['annual_volatility']:.2%} | "
                f"{row['max_drawdown']:.2%} | {sharpe} | {row['excess_annual_return']:.2%} |"
            )
        coverage_lines = []
        for row in coverage.to_dict("records"):
            holding_count = row.get("average_holding_count", pd.NA)
            holding_text = "" if pd.isna(holding_count) else f"{holding_count:.0f}"
            coverage_lines.append(
                f"| {row['factor']} | {row['signal_count']} | {row['first_signal']} | {row['last_signal']} | "
                f"{row['valid_rows']} | {holding_text} |"
            )

        benchmark_count = int(benchmark_returns["benchmark_return"].notna().sum()) if not benchmark_returns.empty else 0
        factor_markdown = _markdown_table(factor_table)
        content = f"""# Excel 单因子月度 Top20% 回测报告

## 样本与口径

- 数据源：只读连接 `data/share_quant.duckdb`，查询现有研究视图与 silver parquet。
- 输出目录：`research/factor_combo/outputs/`。
- 回测评价期：`{self.start}` 至 `{end_date}`；2021 年数据仅用于 60 日动量和 252 日波动率预热。
- 股票池：已上市、未停牌、非 ST。
- 调仓：每月最后交易日收盘后打分，下一交易日收盘成交，收益从成交后的下一个交易日开始计入。
- 持仓：每个单因子买入截面得分前 20%，等权持有到下次调仓。
- 交易成本：单边 `{self.transaction_cost:.2%}`，按调仓换手扣除。
- 基准：中证全指 `{self.benchmark}`，有效收益日数 `{benchmark_count}`。

## 因子定义

{factor_markdown}

## 回测指标

| 因子 | 年化收益 | 年化波动 | 最大回撤 | Sharpe | 年化超额 |
|---|---:|---:|---:|---:|---:|
{chr(10).join(metric_lines)}

## 覆盖率

| 因子 | 调仓次数 | 首个信号日 | 最后信号日 | 有效样本行 | 平均持仓数 |
|---|---:|---:|---:|---:|---:|
{chr(10).join(coverage_lines)}

## 输出文件

- 表格：`outputs/tables/factor_metrics.csv`、`factor_coverage.csv`、`nav_by_factor.csv`、`monthly_returns.csv`、`turnover.csv`、`rebalance_weights.csv`
- 图像：`outputs/images/nav_curve.png`、`drawdown.png`、`metrics_table.png`、`coverage_table.png`

## 注意

财务类和股东集中度因子均按公告可见时间做 as-of 对齐，不使用报告期直接后向填充。持仓期间缺失收益率会区分真实停牌和无效缺失：停牌缺失按 0 收益保留权重；非停牌缺失从当日组合收益中剔除并重配，相关计数和剔除权重已写入指标表。
"""
        path.write_text(content, encoding="utf-8")


    def _write_neutralized_report(
        self,
        path: Path,
        metrics: pd.DataFrame,
        coverage: pd.DataFrame,
        metrics_comparison: pd.DataFrame,
        raw_metrics: pd.DataFrame,
        end_date: str,
        benchmark_returns: pd.DataFrame,
        image_paths: dict[str, Path],
    ) -> None:
        benchmark_count = int(benchmark_returns["benchmark_return"].notna().sum()) if not benchmark_returns.empty else 0
        metric_table = _format_report_numbers(
            metrics.head(11),
            ["factor", "annual_return", "annual_volatility", "max_drawdown", "sharpe", "excess_annual_return"],
        )
        comparison_table = _format_report_numbers(
            metrics_comparison.head(11),
            [
                "factor",
                "annual_return_raw",
                "annual_return_neutralized",
                "annual_return_delta",
                "excess_annual_return_delta",
                "sharpe_delta",
                "max_drawdown_delta",
            ],
        )
        coverage_table = _format_report_numbers(
            coverage,
            ["factor", "signal_count", "first_signal", "last_signal", "valid_rows", "average_holding_count"],
        )
        raw_top = raw_metrics.iloc[0]["factor"] if not raw_metrics.empty else ""
        neutralized_top = metrics.iloc[0]["factor"] if not metrics.empty else ""
        image_lines = "\n".join(f"- `{path_value.relative_to(path.parent).as_posix()}`" for path_value in image_paths.values())
        content = f"""# Industry and Size Neutralized Top20% Backtest Report

## Sample and Method

- Data source: read-only `data/share_quant.duckdb`, existing research views, and silver parquet.
- Output directory: `{path.parent.as_posix()}/`.
- Evaluation window: `{self.start}` to `{end_date}`; warmup starts at `{self.warmup_start}`.
- Signal and execution: month-end close signal, next trading day close execution, held until the next rebalance.
- Universe: listed, not suspended, and non-ST stocks at the execution date.
- Benchmark: `{self.benchmark}`, valid benchmark return days `{benchmark_count}`.
- Transaction cost: one-way `{self.transaction_cost:.2%}`, deducted by rebalance turnover.
- Neutralization: after the existing winsorization and z-score step, regress `score` on `log(total_mv)` and current first-level industry dummies within each `factor + trade_date`; use only the residual `neutralized_score` for Top20% selection.
- Missing exposures: missing industry is `Unknown`; missing `log(total_mv)` is filled by the same cross-section mean, or 0 if the cross-section has no valid size.

## Neutralized Metrics

{_markdown_table(metric_table)}

## Raw vs Neutralized Comparison

{_markdown_table(comparison_table)}

## Coverage

{_markdown_table(coverage_table)}

## Quick Read

- Best raw annual return factor in the paired run: `{raw_top}`.
- Best neutralized annual return factor: `{neutralized_top}`.
- Raw and neutralized runs use the same sample window, universe filter, rebalance calendar, transaction cost, and benchmark.

## Output Files

- Tables: `tables/factor_metrics.csv`, `tables/factor_coverage.csv`, `tables/nav_by_factor.csv`, `tables/monthly_returns.csv`, `tables/turnover.csv`, `tables/rebalance_weights.csv`, `tables/factor_metrics_comparison.csv`
- Images: `images/nav_curve.png`, `images/drawdown.png`, `images/metrics_table.png`, `images/metrics_comparison_table.png`, `images/raw_vs_neutralized_annual_return.png`

## Images

{image_lines}
"""
        path.write_text(content, encoding="utf-8")


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


def _format_report_numbers(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=columns)
    selected = frame[[col for col in columns if col in frame.columns]].copy()
    if "factor" in selected.columns:
        selected["factor"] = selected["factor"].map(lambda value: FACTOR_DISPLAY_NAMES.get(value, value))
    for col in selected.columns:
        if col == "factor":
            continue
        if col in {"signal_count", "valid_rows"}:
            selected[col] = selected[col].map(lambda x: "" if pd.isna(x) else f"{x:.0f}")
        elif col in {"first_signal", "last_signal"}:
            selected[col] = selected[col].map(_format_yyyymmdd)
        elif "sharpe" in col:
            selected[col] = selected[col].map(lambda x: "" if pd.isna(x) else f"{x:.2f}")
        elif "count" in col:
            selected[col] = selected[col].map(lambda x: "" if pd.isna(x) else f"{x:.0f}")
        else:
            selected[col] = selected[col].map(lambda x: "" if pd.isna(x) else f"{x:.2%}")
    return selected


def _format_yyyymmdd(value: object) -> str:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text
