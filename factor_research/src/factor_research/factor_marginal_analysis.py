from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import duckdb
import numpy as np
import pandas as pd
import yaml

from .factor_backtest import (
    DATE_FMT,
    FACTOR_SPECS,
    FactorResearchRunner,
    FactorSpec,
    backtest_monthly_top_quantile,
    build_rebalance_calendar,
    filter_scores_for_execution_universe,
    neutralize_scores,
    normalize_factor_panel,
    one_way_turnover,
    select_top_quantile_weights,
    yyyymmdd,
)
from .factor_diagnostics import (
    assign_score_deciles,
    build_forward_period_returns,
    compute_decile_returns,
    compute_ic_by_period,
    compute_long_short_returns,
)
from .paths import load_paths


FULL_11 = "Full11_Equal"
FIXED_4 = "Fixed4_Equal"
GROUPED_8 = "Grouped8_Equal"
LOO_PREFIX = "LOO_without_"
AOB_PREFIX = "AOB_add_"
OOS_FULL = "OOS_FULL"

PRIMARY_METRICS = ("d10_d1_annual_return", "top20_excess_annual_return")
SUMMARY_METRICS = (
    "d10_d1_annual_return",
    "top20_excess_annual_return",
    "rank_ic_mean",
    "d10_d1_max_drawdown",
    "top20_max_drawdown",
    "d10_d1_average_two_leg_turnover",
    "top20_average_monthly_turnover",
    "average_valid_stock_count",
    "average_available_component_count",
)


@dataclass(frozen=True)
class FactorGroup:
    group_id: str
    name_zh: str
    factor_ids: tuple[str, ...]


@dataclass(frozen=True)
class PeriodSpec:
    period_id: str
    start: str
    end: str | None


@dataclass(frozen=True)
class MarginalAnalysisConfig:
    config_path: Path
    source_factor_config: Path
    artifact_id: str
    locked_at: str
    warmup_start: str
    research_start: str
    train_start: str
    train_end: str
    oos_start: str
    end: str | None
    benchmark: str
    transaction_cost: float
    top_quantile: float
    bucket_count: int
    baseline_min_factor_count: int
    baseline_factor_count: int
    factor_directions: Mapping[str, int]
    fixed_factor_ids: tuple[str, ...]
    groups: tuple[FactorGroup, ...]
    periods: tuple[PeriodSpec, ...]
    required_positive_subperiods: int
    required_negative_subperiods: int

    @property
    def factor_ids(self) -> tuple[str, ...]:
        return tuple(self.factor_directions)

    def minimum_component_count(self, component_count: int) -> int:
        return max(
            1,
            math.ceil(component_count * self.baseline_min_factor_count / self.baseline_factor_count),
        )


@dataclass(frozen=True)
class StrategyDefinition:
    family: str
    strategy_id: str
    factor_id: str | None
    baseline_id: str | None
    factor_ids: tuple[str, ...]
    min_factor_count: int


def load_analysis_config(path: str | Path) -> MarginalAnalysisConfig:
    config_path = Path(path).resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if int(raw.get("schema_version", 0)) != 1:
        raise ValueError("factor marginal config schema_version must be 1")

    source_path = Path(str(raw.get("source_factor_config", "")))
    if not source_path.is_absolute():
        source_path = (config_path.parent / source_path).resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"source factor config does not exist: {source_path}")
    source = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
    factor_pool = source.get("factor_pool") or []
    directions = {str(row["id"]): int(row["direction"]) for row in factor_pool}
    if len(directions) != 11 or any(direction not in {-1, 1} for direction in directions.values()):
        raise ValueError("source factor config must contain 11 unique factors with direction -1 or 1")

    code_directions = {spec.name: spec.direction for spec in FACTOR_SPECS}
    if directions != {factor: code_directions.get(factor) for factor in directions}:
        raise ValueError("source factor config does not match FACTOR_SPECS names and directions")

    fixed = tuple(map(str, raw.get("fixed_factor_pool") or ()))
    if len(fixed) != 4 or len(set(fixed)) != 4 or not set(fixed) < set(directions):
        raise ValueError("fixed_factor_pool must contain four unique factors from the 11-factor pool")

    groups: list[FactorGroup] = []
    grouped_factors: list[str] = []
    group_ids: set[str] = set()
    for row in raw.get("factor_groups") or ():
        group_id = str(row["id"])
        factor_ids = tuple(map(str, row.get("factors") or ()))
        if not factor_ids or group_id in group_ids:
            raise ValueError("each factor group must have a unique id and at least one factor")
        group_ids.add(group_id)
        grouped_factors.extend(factor_ids)
        groups.append(FactorGroup(group_id, str(row.get("name_zh", group_id)), factor_ids))
    if len(groups) != 8 or len(grouped_factors) != len(set(grouped_factors)) or set(grouped_factors) != set(directions):
        raise ValueError("eight factor groups must partition the complete 11-factor pool exactly once")

    protocol = raw.get("protocol") or {}
    periods = tuple(
        PeriodSpec(
            period_id=str(row["id"]),
            start=yyyymmdd(str(row["start"])),
            end=yyyymmdd(str(row["end"])) if row.get("end") else None,
        )
        for row in raw.get("oos_periods") or ()
    )
    if tuple(period.period_id for period in periods) != ("2025H1", "2025H2", "2026YTD"):
        raise ValueError("oos_periods must be exactly 2025H1, 2025H2, and 2026YTD")
    if protocol.get("d10_d1_primary") != "gross_forward_period":
        raise ValueError("only the frozen gross_forward_period D10-D1 protocol is supported")
    if protocol.get("d10_period_attribution") != "execution_month":
        raise ValueError("only execution_month D10 period attribution is supported")
    if protocol.get("group_correlation") != "mean_monthly_cross_sectional_spearman":
        raise ValueError("unsupported group correlation protocol")

    stability = raw.get("stability") or {}
    config = MarginalAnalysisConfig(
        config_path=config_path,
        source_factor_config=source_path,
        artifact_id=str(raw.get("artifact_id", "")),
        locked_at=str(raw.get("locked_at", "")),
        warmup_start=yyyymmdd(str(protocol.get("warmup_start", "2021-01-01"))),
        research_start=yyyymmdd(str(protocol.get("research_start", "2022-01-01"))),
        train_start=yyyymmdd(str(protocol.get("train_start", "2022-01-01"))),
        train_end=yyyymmdd(str(protocol.get("train_end", "2024-12-31"))),
        oos_start=yyyymmdd(str(protocol.get("oos_start", "2025-01-01"))),
        end=yyyymmdd(str(protocol["end"])) if protocol.get("end") else None,
        benchmark=str(protocol.get("benchmark", "000985.CSI")),
        transaction_cost=float(protocol.get("transaction_cost", 0.001)),
        top_quantile=float(protocol.get("top_quantile", 0.20)),
        bucket_count=int(protocol.get("bucket_count", 10)),
        baseline_min_factor_count=int(protocol.get("baseline_min_factor_count", 6)),
        baseline_factor_count=int(protocol.get("baseline_factor_count", 11)),
        factor_directions=directions,
        fixed_factor_ids=fixed,
        groups=tuple(groups),
        periods=periods,
        required_positive_subperiods=int(stability.get("required_positive_subperiods", 2)),
        required_negative_subperiods=int(stability.get("required_negative_subperiods", 2)),
    )
    if config.research_start != config.train_start or config.train_end >= config.oos_start:
        raise ValueError("training and out-of-sample date boundaries overlap or are inconsistent")
    if not 0 < config.top_quantile <= 1 or config.bucket_count != 10:
        raise ValueError("frozen protocol requires top_quantile in (0,1] and exactly 10 buckets")
    if config.baseline_min_factor_count != 6 or config.baseline_factor_count != 11:
        raise ValueError("frozen coverage protocol must be 6/11")
    return config


def build_experiment_definitions(config: MarginalAnalysisConfig) -> tuple[StrategyDefinition, ...]:
    factors = config.factor_ids
    definitions: list[StrategyDefinition] = [
        StrategyDefinition("baseline", FULL_11, None, None, factors, config.minimum_component_count(len(factors)))
    ]
    definitions.extend(
        StrategyDefinition(
            "leave_one_out",
            f"{LOO_PREFIX}{factor}",
            factor,
            FULL_11,
            tuple(item for item in factors if item != factor),
            config.minimum_component_count(len(factors) - 1),
        )
        for factor in factors
    )
    definitions.append(
        StrategyDefinition(
            "baseline",
            FIXED_4,
            None,
            None,
            config.fixed_factor_ids,
            config.minimum_component_count(len(config.fixed_factor_ids)),
        )
    )
    excluded = tuple(factor for factor in factors if factor not in config.fixed_factor_ids)
    definitions.extend(
        StrategyDefinition(
            "add_one_back",
            f"{AOB_PREFIX}{factor}",
            factor,
            FIXED_4,
            (*config.fixed_factor_ids, factor),
            config.minimum_component_count(len(config.fixed_factor_ids) + 1),
        )
        for factor in excluded
    )
    return tuple(definitions)


def build_equal_composite_scores(
    scores: pd.DataFrame,
    factor_ids: Sequence[str],
    composite_name: str,
    min_factor_count: int,
    score_col: str = "neutralized_score",
) -> pd.DataFrame:
    required = {"factor", "trade_date", "ts_code", score_col}
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"scores are missing columns: {sorted(missing)}")
    factors = tuple(map(str, factor_ids))
    if len(factors) != len(set(factors)) or min_factor_count < 1 or min_factor_count > len(factors):
        raise ValueError("factor ids must be unique and min_factor_count must fit the factor pool")
    base = scores[scores["factor"].isin(factors)]
    if base.empty:
        return pd.DataFrame(
            columns=[
                "factor",
                "trade_date",
                "ts_code",
                "score",
                "available_factor_count",
                "available_component_count",
                "configured_component_count",
                "available_weight_sum",
                "weight_source",
            ]
        )
    matrix = (
        base.pivot_table(index=["trade_date", "ts_code"], columns="factor", values=score_col, aggfunc="mean")
        .reindex(columns=factors)
        .apply(pd.to_numeric, errors="coerce")
    )
    valid_count = matrix.notna().sum(axis=1)
    values = matrix.mean(axis=1, skipna=True).where(valid_count >= min_factor_count)
    result = values.rename("score").reset_index()
    result.insert(0, "factor", composite_name)
    result["available_factor_count"] = valid_count.to_numpy(dtype=int)
    result["available_component_count"] = valid_count.to_numpy(dtype=int)
    result["configured_component_count"] = len(factors)
    result["available_weight_sum"] = np.where(result["score"].notna(), 1.0, 0.0)
    result["weight_source"] = "equal_available_factors"
    return result


def build_grouped_composite_scores(
    scores: pd.DataFrame,
    groups: Sequence[FactorGroup],
    composite_name: str,
    min_group_count: int,
    score_col: str = "neutralized_score",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    group_frames: list[pd.DataFrame] = []
    for group in groups:
        frame = build_equal_composite_scores(
            scores,
            group.factor_ids,
            group.group_id,
            min_factor_count=1,
            score_col=score_col,
        )
        if frame.empty:
            continue
        frame = frame.rename(columns={"available_factor_count": "group_available_factor_count"})
        group_frames.append(
            frame[
                [
                    "factor",
                    "trade_date",
                    "ts_code",
                    "score",
                    "group_available_factor_count",
                    "configured_component_count",
                ]
            ]
        )
    if not group_frames:
        empty = pd.DataFrame(columns=["factor", "trade_date", "ts_code", "score"])
        return empty, empty
    group_scores = pd.concat(group_frames, ignore_index=True)
    composite_input = group_scores.rename(columns={"score": "group_score"})
    composite = build_equal_composite_scores(
        composite_input,
        [group.group_id for group in groups],
        composite_name,
        min_factor_count=min_group_count,
        score_col="group_score",
    )
    return composite, group_scores


def build_all_composite_scores(
    scores: pd.DataFrame,
    definitions: Sequence[StrategyDefinition],
    groups: Sequence[FactorGroup],
    grouped_name: str,
    min_group_count: int,
    all_factor_ids: Sequence[str],
    score_col: str = "neutralized_score",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    required = {"factor", "trade_date", "ts_code", score_col}
    if required - set(scores.columns):
        raise ValueError(f"scores are missing columns: {sorted(required - set(scores.columns))}")
    matrix = (
        scores[scores["factor"].isin(all_factor_ids)]
        .pivot_table(
            index=["trade_date", "ts_code"],
            columns="factor",
            values=score_col,
            aggfunc="mean",
        )
        .reindex(columns=all_factor_ids)
        .apply(pd.to_numeric, errors="coerce")
    )
    composite_frames = [
        _composite_from_matrix(
            matrix,
            definition.factor_ids,
            definition.strategy_id,
            definition.min_factor_count,
            "equal_available_factors",
        )
        for definition in definitions
    ]
    group_frames = [
        _composite_from_matrix(
            matrix,
            group.factor_ids,
            group.group_id,
            1,
            "equal_available_factors_within_group",
        ).rename(columns={"available_factor_count": "group_available_factor_count"})
        for group in groups
    ]
    group_scores = pd.concat(group_frames, ignore_index=True)
    group_matrix = (
        group_scores.pivot_table(
            index=["trade_date", "ts_code"],
            columns="factor",
            values="score",
            aggfunc="mean",
        )
        .reindex(columns=[group.group_id for group in groups])
        .apply(pd.to_numeric, errors="coerce")
    )
    grouped = _composite_from_matrix(
        group_matrix,
        [group.group_id for group in groups],
        grouped_name,
        min_group_count,
        "equal_available_groups",
    )
    return pd.concat([*composite_frames, grouped], ignore_index=True), group_scores


def _composite_from_matrix(
    matrix: pd.DataFrame,
    component_ids: Sequence[str],
    composite_name: str,
    min_component_count: int,
    weight_source: str,
) -> pd.DataFrame:
    cross = matrix.reindex(columns=list(component_ids))
    valid_count = cross.notna().sum(axis=1)
    values = cross.mean(axis=1, skipna=True).where(valid_count >= min_component_count)
    result = values.rename("score").reset_index()
    result.insert(0, "factor", composite_name)
    result["available_factor_count"] = valid_count.to_numpy(dtype=int)
    result["available_component_count"] = valid_count.to_numpy(dtype=int)
    result["configured_component_count"] = len(component_ids)
    result["available_weight_sum"] = np.where(result["score"].notna(), 1.0, 0.0)
    result["weight_source"] = weight_source
    return result


def compute_group_correlation_matrix(
    group_scores: pd.DataFrame,
    group_ids: Sequence[str],
    train_start: str,
    train_end: str,
) -> pd.DataFrame:
    required = {"factor", "trade_date", "ts_code", "score"}
    if required - set(group_scores.columns):
        raise ValueError("group_scores must include factor, trade_date, ts_code, score")
    train = group_scores[
        group_scores["trade_date"].astype(str).between(str(train_start), str(train_end), inclusive="both")
    ]
    matrices: list[pd.DataFrame] = []
    for _date, group in train.groupby("trade_date", sort=True):
        cross = group.pivot_table(index="ts_code", columns="factor", values="score", aggfunc="mean")
        matrices.append(cross.corr(method="spearman", min_periods=3).reindex(index=group_ids, columns=group_ids))
    if not matrices:
        return pd.DataFrame(index=group_ids, columns=group_ids, dtype=float)
    stacked = np.stack([matrix.to_numpy(dtype=float) for matrix in matrices])
    with np.errstate(invalid="ignore"):
        mean_values = np.nanmean(stacked, axis=0)
    result = pd.DataFrame(mean_values, index=group_ids, columns=group_ids)
    np.fill_diagonal(result.values, 1.0)
    result.index.name = "group_id"
    return result


def build_bucket_turnover(
    deciles: pd.DataFrame,
    bucket_count: int = 10,
) -> pd.DataFrame:
    required = {"factor", "trade_date", "ts_code", "decile"}
    if required - set(deciles.columns):
        raise ValueError("deciles must include factor, trade_date, ts_code, decile")
    rows: list[dict[str, object]] = []
    for factor, factor_rows in deciles.groupby("factor", sort=True):
        previous: dict[int, pd.Series | None] = {1: None, bucket_count: None}
        for trade_date, current_rows in factor_rows.groupby("trade_date", sort=True):
            for bucket in (1, bucket_count):
                selected = current_rows[current_rows["decile"] == bucket]
                current = pd.Series(
                    1.0 / len(selected) if len(selected) else dtype_float_nan(),
                    index=selected["ts_code"].astype(str),
                    dtype="float64",
                )
                turnover = one_way_turnover(previous[bucket], current)
                rows.append(
                    {
                        "factor": factor,
                        "signal_date": str(trade_date),
                        "bucket": bucket,
                        "turnover": turnover,
                        "holding_count": int(len(selected)),
                    }
                )
                previous[bucket] = current
    result = pd.DataFrame(rows)
    if result.empty:
        return pd.DataFrame(
            columns=["factor", "signal_date", "d10_turnover", "d1_turnover", "two_leg_turnover"]
        )
    pivot = result.pivot_table(
        index=["factor", "signal_date"], columns="bucket", values="turnover", aggfunc="first"
    ).reset_index()
    pivot["d1_turnover"] = pivot.get(1, pd.Series(0.0, index=pivot.index))
    pivot["d10_turnover"] = pivot.get(bucket_count, pd.Series(0.0, index=pivot.index))
    pivot["two_leg_turnover"] = pivot["d1_turnover"] + pivot["d10_turnover"]
    return pivot[["factor", "signal_date", "d10_turnover", "d1_turnover", "two_leg_turnover"]]


def dtype_float_nan() -> float:
    return float("nan")


def attach_execution_dates(frame: pd.DataFrame, calendar: pd.DataFrame, date_col: str) -> pd.DataFrame:
    required = {"signal_date", "exec_date"}
    if required - set(calendar.columns):
        raise ValueError("calendar must include signal_date and exec_date")
    result = frame.copy()
    mapping = calendar[["signal_date", "exec_date"]].drop_duplicates().copy()
    mapping["signal_date"] = mapping["signal_date"].astype(str)
    mapping["exec_date"] = mapping["exec_date"].astype(str)
    result[date_col] = result[date_col].astype(str)
    if "exec_date" not in result.columns:
        result = result.merge(mapping, left_on=date_col, right_on="signal_date", how="left")
        if date_col != "signal_date":
            result = result.drop(columns=["signal_date"])
    else:
        result["exec_date"] = result["exec_date"].astype(str)
    result["attribution_date"] = result["exec_date"]
    result["attribution_month"] = pd.to_datetime(
        result["attribution_date"], format=DATE_FMT, errors="coerce"
    ).dt.to_period("M").astype("string")
    return result


def build_monthly_strategy_results(
    strategy_ids: Sequence[str],
    long_short_returns: pd.DataFrame,
    rank_ic: pd.DataFrame,
    top20_daily: pd.DataFrame,
    top20_turnover: pd.DataFrame,
    two_leg_turnover: pd.DataFrame,
    calendar: pd.DataFrame,
    oos_start: str,
    end_date: str,
) -> pd.DataFrame:
    long_short = attach_execution_dates(
        long_short_returns.rename(columns={"trade_date": "score_date"}),
        calendar,
        "score_date",
    )
    ic = attach_execution_dates(
        rank_ic.rename(columns={"trade_date": "score_date"}),
        calendar,
        "score_date",
    )
    top_turn = attach_execution_dates(top20_turnover, calendar, "signal_date")
    two_turn = attach_execution_dates(two_leg_turnover, calendar, "signal_date")

    daily = top20_daily.copy()
    daily["trade_date"] = daily["trade_date"].astype(str)
    daily["attribution_month"] = pd.to_datetime(
        daily["trade_date"], format=DATE_FMT, errors="coerce"
    ).dt.to_period("M").astype("string")
    daily = daily[daily["trade_date"].between(oos_start, end_date, inclusive="both")]
    top_monthly = (
        daily.groupby(["factor", "attribution_month"], as_index=False)
        .agg(
            top20_return=("portfolio_return", _compound),
            benchmark_return=("benchmark_return", _compound),
            top20_within_month_max_drawdown=("portfolio_return", _max_drawdown),
        )
    )
    top_monthly["top20_excess_return"] = (
        top_monthly["top20_return"] - top_monthly["benchmark_return"]
    )

    d10 = (
        long_short[
            long_short["attribution_date"].astype(str).between(oos_start, end_date, inclusive="both")
        ]
        .groupby(["factor", "attribution_month"], as_index=False)
        .agg(d10_d1_return=("long_short_return", _compound))
    )
    d10["d10_d1_month_drawdown"] = d10["d10_d1_return"].clip(upper=0.0)
    monthly_ic = (
        ic[ic["attribution_date"].astype(str).between(oos_start, end_date, inclusive="both")]
        .groupby(["factor", "attribution_month"], as_index=False)
        .agg(rank_ic=("rank_ic", "mean"))
    )
    top_monthly_turnover = (
        top_turn[
            top_turn["attribution_date"].astype(str).between(oos_start, end_date, inclusive="both")
        ]
        .groupby(["factor", "attribution_month"], as_index=False)
        .agg(top20_turnover=("turnover", "sum"))
    )
    d10_monthly_turnover = (
        two_turn[
            two_turn["attribution_date"].astype(str).between(oos_start, end_date, inclusive="both")
        ]
        .groupby(["factor", "attribution_month"], as_index=False)
        .agg(d10_d1_two_leg_turnover=("two_leg_turnover", "sum"))
    )

    months = pd.period_range(
        pd.Timestamp(oos_start).to_period("M"),
        pd.Timestamp(end_date).to_period("M"),
        freq="M",
    ).astype(str)
    scaffold = pd.MultiIndex.from_product(
        [strategy_ids, months], names=["factor", "attribution_month"]
    ).to_frame(index=False)
    result = scaffold
    for frame in (d10, top_monthly, monthly_ic, top_monthly_turnover, d10_monthly_turnover):
        result = result.merge(frame, on=["factor", "attribution_month"], how="left")
    return result.sort_values(["factor", "attribution_month"]).reset_index(drop=True)


def summarize_strategy_metrics(
    strategy_ids: Sequence[str],
    periods: Sequence[PeriodSpec],
    end_date: str,
    long_short_returns: pd.DataFrame,
    rank_ic: pd.DataFrame,
    top20_daily: pd.DataFrame,
    top20_turnover: pd.DataFrame,
    two_leg_turnover: pd.DataFrame,
    composite_scores: pd.DataFrame,
    calendar: pd.DataFrame,
    execution_universe: pd.DataFrame,
    oos_start: str,
) -> pd.DataFrame:
    long_short = attach_execution_dates(
        long_short_returns.rename(columns={"trade_date": "score_date"}),
        calendar,
        "score_date",
    )
    ic = attach_execution_dates(
        rank_ic.rename(columns={"trade_date": "score_date"}),
        calendar,
        "score_date",
    )
    top_turn = attach_execution_dates(top20_turnover, calendar, "signal_date")
    two_turn = attach_execution_dates(two_leg_turnover, calendar, "signal_date")
    coverage = attach_execution_dates(
        composite_scores[
            [
                "factor",
                "trade_date",
                "ts_code",
                "score",
                "available_component_count",
            ]
        ].rename(columns={"trade_date": "score_date"}),
        calendar,
        "score_date",
    )
    daily = top20_daily.copy()
    daily["trade_date"] = daily["trade_date"].astype(str)

    scopes = [PeriodSpec(OOS_FULL, oos_start, end_date), *periods]
    rows: list[dict[str, object]] = []
    universe_counts = (
        execution_universe.groupby("exec_date")["ts_code"].nunique().rename("execution_universe_count")
    )
    for scope in scopes:
        scope_end = min(scope.end or end_date, end_date)
        for strategy in strategy_ids:
            ls = long_short[
                (long_short["factor"] == strategy)
                & long_short["attribution_date"].astype(str).between(
                    scope.start, scope_end, inclusive="both"
                )
            ]
            ls_series = pd.to_numeric(ls["long_short_return"], errors="coerce").dropna()
            top = daily[
                (daily["factor"] == strategy)
                & daily["trade_date"].between(scope.start, scope_end, inclusive="both")
            ].sort_values("trade_date")
            top_series = pd.to_numeric(top["portfolio_return"], errors="coerce").dropna()
            bench_series = pd.to_numeric(top["benchmark_return"], errors="coerce").dropna()
            current_ic = ic[
                (ic["factor"] == strategy)
                & ic["attribution_date"].astype(str).between(scope.start, scope_end, inclusive="both")
            ]
            current_top_turn = top_turn[
                (top_turn["factor"] == strategy)
                & top_turn["attribution_date"].astype(str).between(
                    scope.start, scope_end, inclusive="both"
                )
            ]
            current_two_turn = two_turn[
                (two_turn["factor"] == strategy)
                & two_turn["attribution_date"].astype(str).between(
                    scope.start, scope_end, inclusive="both"
                )
            ]
            current_coverage = coverage[
                (coverage["factor"] == strategy)
                & coverage["attribution_date"].astype(str).between(
                    scope.start, scope_end, inclusive="both"
                )
            ].copy()
            valid_by_date = (
                current_coverage.assign(is_valid=current_coverage["score"].notna())
                .groupby("exec_date", as_index=False)
                .agg(
                    valid_stock_count=("is_valid", "sum"),
                    average_available_component_count=("available_component_count", "mean"),
                )
            )
            if not valid_by_date.empty:
                valid_by_date["execution_universe_count"] = valid_by_date["exec_date"].map(
                    universe_counts
                )
                valid_by_date["valid_stock_coverage"] = (
                    valid_by_date["valid_stock_count"]
                    / valid_by_date["execution_universe_count"]
                )
            d10_annual = _periodic_annual_return(ls_series)
            top_annual = _daily_annual_return(top_series)
            benchmark_annual = _daily_annual_return(bench_series)
            rows.append(
                {
                    "period": scope.period_id,
                    "period_start": scope.start,
                    "period_end": scope_end,
                    "strategy_id": strategy,
                    "d10_d1_period_count": int(len(ls_series)),
                    "d10_d1_annual_return": d10_annual,
                    "d10_d1_cumulative_return": _compound(ls_series),
                    "d10_d1_max_drawdown": _max_drawdown(ls_series),
                    "d10_d1_average_two_leg_turnover": _mean_or_nan(
                        current_two_turn["two_leg_turnover"]
                    ),
                    "top20_trading_days": int(len(top_series)),
                    "top20_annual_return": top_annual,
                    "benchmark_annual_return": benchmark_annual,
                    "top20_excess_annual_return": (
                        top_annual - benchmark_annual
                        if pd.notna(top_annual) and pd.notna(benchmark_annual)
                        else np.nan
                    ),
                    "top20_cumulative_return": _compound(top_series),
                    "top20_max_drawdown": _max_drawdown(top_series),
                    "top20_average_monthly_turnover": _mean_or_nan(
                        current_top_turn["turnover"]
                    ),
                    "rank_ic_mean": _mean_or_nan(current_ic["rank_ic"]),
                    "rank_ic_periods": int(
                        pd.to_numeric(current_ic["rank_ic"], errors="coerce").notna().sum()
                    ),
                    "average_valid_stock_count": _mean_or_nan(
                        valid_by_date.get("valid_stock_count", pd.Series(dtype=float))
                    ),
                    "average_valid_stock_coverage": _mean_or_nan(
                        valid_by_date.get("valid_stock_coverage", pd.Series(dtype=float))
                    ),
                    "average_available_component_count": _mean_or_nan(
                        valid_by_date.get(
                            "average_available_component_count", pd.Series(dtype=float)
                        )
                    ),
                }
            )
    return pd.DataFrame(rows).sort_values(["period", "strategy_id"]).reset_index(drop=True)


def build_marginal_contributions(
    definitions: Sequence[StrategyDefinition],
    metrics: pd.DataFrame,
    periods: Sequence[str],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    indexed = metrics.set_index(["period", "strategy_id"])
    for definition in definitions:
        if definition.family not in {"leave_one_out", "add_one_back"}:
            continue
        if definition.baseline_id is None or definition.factor_id is None:
            continue
        if definition.family == "leave_one_out":
            included_id, excluded_id = definition.baseline_id, definition.strategy_id
        else:
            included_id, excluded_id = definition.strategy_id, definition.baseline_id
        for period in periods:
            included = indexed.loc[(period, included_id)]
            excluded = indexed.loc[(period, excluded_id)]
            row: dict[str, object] = {
                "experiment": definition.family,
                "factor": definition.factor_id,
                "period": period,
                "included_strategy": included_id,
                "excluded_strategy": excluded_id,
            }
            for metric in SUMMARY_METRICS:
                included_value = pd.to_numeric(
                    pd.Series([included.get(metric)]), errors="coerce"
                ).iloc[0]
                excluded_value = pd.to_numeric(
                    pd.Series([excluded.get(metric)]), errors="coerce"
                ).iloc[0]
                row[f"included_{metric}"] = included_value
                row[f"excluded_{metric}"] = excluded_value
                row[f"{metric}_delta"] = (
                    included_value - excluded_value
                    if pd.notna(included_value) and pd.notna(excluded_value)
                    else np.nan
                )
            rows.append(row)
    return pd.DataFrame(rows)


def build_monthly_marginal_contributions(
    definitions: Sequence[StrategyDefinition],
    monthly: pd.DataFrame,
) -> pd.DataFrame:
    indexed = monthly.set_index(["factor", "attribution_month"])
    months = sorted(monthly["attribution_month"].dropna().astype(str).unique())
    metric_columns = (
        "d10_d1_return",
        "top20_return",
        "benchmark_return",
        "top20_excess_return",
        "rank_ic",
        "d10_d1_month_drawdown",
        "top20_within_month_max_drawdown",
        "d10_d1_two_leg_turnover",
        "top20_turnover",
    )
    rows: list[dict[str, object]] = []
    for definition in definitions:
        if definition.family not in {"leave_one_out", "add_one_back"}:
            continue
        included_id = definition.baseline_id if definition.family == "leave_one_out" else definition.strategy_id
        excluded_id = definition.strategy_id if definition.family == "leave_one_out" else definition.baseline_id
        assert included_id is not None and excluded_id is not None and definition.factor_id is not None
        for month in months:
            included = indexed.loc[(included_id, month)]
            excluded = indexed.loc[(excluded_id, month)]
            row: dict[str, object] = {
                "experiment": definition.family,
                "factor": definition.factor_id,
                "month": month,
                "included_strategy": included_id,
                "excluded_strategy": excluded_id,
            }
            for metric in metric_columns:
                left = pd.to_numeric(pd.Series([included.get(metric)]), errors="coerce").iloc[0]
                right = pd.to_numeric(pd.Series([excluded.get(metric)]), errors="coerce").iloc[0]
                row[f"included_{metric}"] = left
                row[f"excluded_{metric}"] = right
                row[f"{metric}_delta"] = (
                    left - right if pd.notna(left) and pd.notna(right) else np.nan
                )
            rows.append(row)
    return pd.DataFrame(rows)


def classify_stability(
    contributions: pd.DataFrame,
    config: MarginalAnalysisConfig,
) -> pd.DataFrame:
    full = contributions[contributions["period"] == OOS_FULL].copy()
    subperiods = contributions[contributions["period"].isin([period.period_id for period in config.periods])]
    for metric in PRIMARY_METRICS:
        delta_col = f"{metric}_delta"
        counts = (
            subperiods.assign(
                positive=lambda frame: pd.to_numeric(frame[delta_col], errors="coerce") > 0,
                negative=lambda frame: pd.to_numeric(frame[delta_col], errors="coerce") < 0,
            )
            .groupby(["experiment", "factor"], as_index=False)
            .agg(
                **{
                    f"{metric}_positive_subperiods": ("positive", "sum"),
                    f"{metric}_negative_subperiods": ("negative", "sum"),
                    f"{metric}_valid_subperiods": (delta_col, "count"),
                }
            )
        )
        full = full.merge(counts, on=["experiment", "factor"], how="left")

        def label(row: pd.Series) -> str:
            full_delta = pd.to_numeric(pd.Series([row.get(delta_col)]), errors="coerce").iloc[0]
            positive = int(row.get(f"{metric}_positive_subperiods", 0) or 0)
            negative = int(row.get(f"{metric}_negative_subperiods", 0) or 0)
            if pd.notna(full_delta) and full_delta > 0 and positive >= config.required_positive_subperiods:
                return "stable_positive"
            if pd.notna(full_delta) and full_delta < 0 and negative >= config.required_negative_subperiods:
                return "stable_negative"
            return "mixed"

        full[f"{metric}_stability"] = full.apply(label, axis=1)
    return full.drop(columns=["period"]).reset_index(drop=True)


def factor_group_definition(config: MarginalAnalysisConfig) -> pd.DataFrame:
    specs = {spec.name: spec for spec in FACTOR_SPECS}
    rows = []
    group_count = len(config.groups)
    for group in config.groups:
        for factor in group.factor_ids:
            spec = specs[factor]
            rows.append(
                {
                    "factor": factor,
                    "direction": config.factor_directions[factor],
                    "original_category": spec.category,
                    "group_id": group.group_id,
                    "group_name_zh": group.name_zh,
                    "group_factor_count": len(group.factor_ids),
                    "within_group_weight_full_data": 1.0 / len(group.factor_ids),
                    "between_group_weight_full_data": 1.0 / group_count,
                    "effective_factor_weight_full_data": 1.0
                    / (group_count * len(group.factor_ids)),
                    "fixed_pool_member": factor in config.fixed_factor_ids,
                }
            )
    return pd.DataFrame(rows)


class FactorMarginalAnalysisRunner:
    def __init__(
        self,
        project_root: Path,
        db_path: Path,
        output_dir: Path,
        config: MarginalAnalysisConfig,
        end: str | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.db_path = Path(db_path).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.config = config
        self.end = yyyymmdd(end) if end else config.end
        self.factor_project_root = Path(__file__).resolve().parents[2]
        self.workspace_root = self.factor_project_root.parent
        self._research = FactorResearchRunner(
            project_root=self.project_root,
            db_path=self.db_path,
            output_dir=self.output_dir,
            start=_date_with_dashes(config.research_start),
            end=_date_with_dashes(self.end) if self.end else None,
            warmup_start=_date_with_dashes(config.warmup_start),
            benchmark=config.benchmark,
            transaction_cost=config.transaction_cost,
        )

    def run(self) -> dict[str, Path]:
        protected_before = snapshot_protected_files(self.workspace_root, self.output_dir)
        git_before = _git_status(self.workspace_root)

        with duckdb.connect(str(self.db_path), read_only=True) as con:
            end_date = self.end or self._research._latest_trade_date(con)
            all_trade_dates = self._research._load_trade_dates(
                con, self.config.warmup_start, end_date
            )
            calendar = build_rebalance_calendar(
                all_trade_dates, self.config.research_start, end_date
            )
            signal_dates = calendar["signal_date"].drop_duplicates().reset_index(drop=True)
            con.register("signal_dates", pd.DataFrame({"trade_date": signal_dates}))
            raw = self._research._load_factor_panel(con, signal_dates, end_date)
            execution_universe = self._research._load_execution_universe(con, calendar)
            exposures = self._research._load_exposures(con, signal_dates)
            returns = self._research._load_returns(
                con, self.config.research_start, end_date
            )
            suspensions = self._research._load_suspensions(
                con, self.config.research_start, end_date
            )
            benchmark = self._research._load_benchmark(
                con, self.config.warmup_start, end_date
            )

        specs = tuple(
            FactorSpec(
                name=factor,
                category="factor_marginal_locked",
                chinese_name=factor,
                formula="loaded_from_existing_factor_implementation",
                direction=direction,
            )
            for factor, direction in self.config.factor_directions.items()
        )
        raw = raw[raw["factor"].isin(self.config.factor_ids)]
        scores = normalize_factor_panel(raw, specs=specs)
        neutralized = neutralize_scores(scores, exposures)
        tradable_scores = filter_scores_for_execution_universe(
            neutralized, calendar, execution_universe
        )

        definitions = build_experiment_definitions(self.config)
        composite_scores, group_scores = build_all_composite_scores(
            tradable_scores,
            definitions,
            self.config.groups,
            GROUPED_8,
            self.config.minimum_component_count(len(self.config.groups)),
            self.config.factor_ids,
        )
        strategy_ids = [definition.strategy_id for definition in definitions] + [GROUPED_8]

        forward_returns = build_forward_period_returns(
            returns,
            calendar,
            end_date,
            ts_codes=tradable_scores["ts_code"].dropna().astype(str).unique(),
            suspensions=suspensions,
        )
        deciles = assign_score_deciles(
            composite_scores,
            bucket_count=self.config.bucket_count,
            score_col="score",
        )
        layer_returns = compute_decile_returns(deciles, forward_returns)
        long_short = compute_long_short_returns(
            layer_returns, bucket_count=self.config.bucket_count
        )
        rank_ic = compute_ic_by_period(
            composite_scores, forward_returns, score_col="score"
        )
        two_leg_turnover = build_bucket_turnover(
            deciles, bucket_count=self.config.bucket_count
        )

        top_weights = select_top_quantile_weights(
            composite_scores, quantile=self.config.top_quantile, score_col="score"
        )
        top20_daily, top20_turnover = backtest_monthly_top_quantile(
            top_weights,
            returns,
            calendar,
            end_date=end_date,
            transaction_cost=self.config.transaction_cost,
            suspensions=suspensions,
        )
        benchmark_daily = benchmark[["trade_date", "benchmark_return"]].copy()
        benchmark_daily["trade_date"] = benchmark_daily["trade_date"].astype(str)
        top20_daily["trade_date"] = top20_daily["trade_date"].astype(str)
        top20_daily = top20_daily.merge(benchmark_daily, on="trade_date", how="left")
        top20_daily["benchmark_return"] = pd.to_numeric(
            top20_daily["benchmark_return"], errors="coerce"
        ).fillna(0.0)
        top20_daily["excess_return"] = (
            top20_daily["portfolio_return"] - top20_daily["benchmark_return"]
        )

        metrics = summarize_strategy_metrics(
            strategy_ids=strategy_ids,
            periods=self.config.periods,
            end_date=end_date,
            long_short_returns=long_short,
            rank_ic=rank_ic,
            top20_daily=top20_daily,
            top20_turnover=top20_turnover,
            two_leg_turnover=two_leg_turnover,
            composite_scores=composite_scores,
            calendar=calendar,
            execution_universe=execution_universe,
            oos_start=self.config.oos_start,
        )
        period_ids = [OOS_FULL, *[period.period_id for period in self.config.periods]]
        contributions = build_marginal_contributions(definitions, metrics, period_ids)
        summary = classify_stability(contributions, self.config)
        loo_summary = summary[summary["experiment"] == "leave_one_out"].reset_index(
            drop=True
        )
        aob_summary = summary[summary["experiment"] == "add_one_back"].reset_index(
            drop=True
        )
        period_contribution = contributions[
            contributions["period"] != OOS_FULL
        ].reset_index(drop=True)
        monthly = build_monthly_strategy_results(
            strategy_ids,
            long_short,
            rank_ic,
            top20_daily,
            top20_turnover,
            two_leg_turnover,
            calendar,
            self.config.oos_start,
            end_date,
        )
        monthly_contribution = build_monthly_marginal_contributions(
            definitions, monthly
        )

        grouped_ids = [FULL_11, FIXED_4, GROUPED_8]
        grouped_metrics = metrics[metrics["strategy_id"].isin(grouped_ids)].reset_index(
            drop=True
        )
        grouped_layer = attach_execution_dates(
            layer_returns[layer_returns["factor"].isin(grouped_ids)].rename(
                columns={"trade_date": "score_date"}
            ),
            calendar,
            "score_date",
        )
        grouped_top20 = top20_daily[
            top20_daily["factor"].isin(grouped_ids)
            & top20_daily["trade_date"].between(
                self.config.oos_start, end_date, inclusive="both"
            )
        ].reset_index(drop=True)
        group_correlation = compute_group_correlation_matrix(
            group_scores,
            [group.group_id for group in self.config.groups],
            self.config.train_start,
            self.config.train_end,
        )
        group_definition = factor_group_definition(self.config)

        paths = self._write_outputs(
            end_date=end_date,
            definitions=definitions,
            loo_summary=loo_summary,
            aob_summary=aob_summary,
            monthly_contribution=monthly_contribution,
            period_contribution=period_contribution,
            group_definition=group_definition,
            group_correlation=group_correlation,
            grouped_metrics=grouped_metrics,
            grouped_layer=grouped_layer,
            grouped_top20=grouped_top20,
            composite_scores=composite_scores,
            top20_turnover=top20_turnover,
        )

        protected_after = snapshot_protected_files(self.workspace_root, self.output_dir)
        audit = compare_protected_snapshots(protected_before, protected_after)
        audit.to_csv(paths["protected_file_audit"], index=False, encoding="utf-8-sig")
        git_after = _git_status(self.workspace_root)
        manifest = self._manifest(
            end_date=end_date,
            definitions=definitions,
            audit=audit,
            git_before=git_before,
            git_after=git_after,
        )
        paths["run_manifest"].write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not audit.empty and not audit["unchanged"].all():
            changed = audit.loc[~audit["unchanged"], "path"].tolist()
            raise RuntimeError(f"protected files changed during analysis: {changed[:10]}")
        return paths

    def _write_outputs(
        self,
        *,
        end_date: str,
        definitions: Sequence[StrategyDefinition],
        loo_summary: pd.DataFrame,
        aob_summary: pd.DataFrame,
        monthly_contribution: pd.DataFrame,
        period_contribution: pd.DataFrame,
        group_definition: pd.DataFrame,
        group_correlation: pd.DataFrame,
        grouped_metrics: pd.DataFrame,
        grouped_layer: pd.DataFrame,
        grouped_top20: pd.DataFrame,
        composite_scores: pd.DataFrame,
        top20_turnover: pd.DataFrame,
    ) -> dict[str, Path]:
        images = self.output_dir / "images"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        images.mkdir(parents=True, exist_ok=True)
        filenames = {
            "leave_one_out_summary": "leave_one_out_summary.csv",
            "add_one_back_summary": "add_one_back_summary.csv",
            "monthly_marginal_contribution": "monthly_marginal_contribution.csv",
            "period_marginal_contribution": "period_marginal_contribution.csv",
            "factor_group_definition": "factor_group_definition.csv",
            "group_correlation_matrix": "group_correlation_matrix.csv",
            "grouped_composite_metrics": "grouped_composite_metrics.csv",
            "grouped_layer_returns": "grouped_layer_returns.csv",
            "grouped_top20_returns": "grouped_top20_returns.csv",
            "coverage_diagnostics": "coverage_diagnostics.csv",
            "protected_file_audit": "protected_file_audit.csv",
            "run_manifest": "run_manifest.json",
            "analysis_report": "analysis_report.md",
        }
        paths = {
            key: _safe_output_path(self.output_dir, filename)
            for key, filename in filenames.items()
        }
        frames = {
            "leave_one_out_summary": loo_summary,
            "add_one_back_summary": aob_summary,
            "monthly_marginal_contribution": monthly_contribution,
            "period_marginal_contribution": period_contribution,
            "factor_group_definition": group_definition,
            "group_correlation_matrix": group_correlation.reset_index(),
            "grouped_composite_metrics": grouped_metrics,
            "grouped_layer_returns": grouped_layer,
            "grouped_top20_returns": grouped_top20,
            "coverage_diagnostics": _coverage_diagnostics(composite_scores),
        }
        for key, frame in frames.items():
            frame.to_csv(paths[key], index=False, encoding="utf-8-sig")

        image_paths = _write_images(
            images,
            loo_summary,
            aob_summary,
            period_contribution,
            group_correlation,
            grouped_metrics,
        )
        paths.update(image_paths)
        report = build_analysis_report(
            config=self.config,
            end_date=end_date,
            loo_summary=loo_summary,
            aob_summary=aob_summary,
            period_contribution=period_contribution,
            grouped_metrics=grouped_metrics,
            group_correlation=group_correlation,
        )
        paths["analysis_report"].write_text(report, encoding="utf-8")
        return paths

    def _manifest(
        self,
        *,
        end_date: str,
        definitions: Sequence[StrategyDefinition],
        audit: pd.DataFrame,
        git_before: str,
        git_after: str,
    ) -> dict[str, object]:
        return {
            "artifact_id": self.config.artifact_id,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_end_date": end_date,
            "database_path": str(self.db_path),
            "database_open_mode": "read_only",
            "config_path": str(self.config.config_path),
            "config_sha256": _sha256(self.config.config_path),
            "source_factor_config": str(self.config.source_factor_config),
            "source_factor_config_sha256": _sha256(self.config.source_factor_config),
            "factor_ids": list(self.config.factor_ids),
            "fixed_factor_ids": list(self.config.fixed_factor_ids),
            "strategy_count": len(definitions) + 1,
            "leave_one_out_count": sum(
                definition.family == "leave_one_out" for definition in definitions
            ),
            "add_one_back_count": sum(
                definition.family == "add_one_back" for definition in definitions
            ),
            "protected_file_audit_passed": bool(
                audit.empty or audit["unchanged"].all()
            ),
            "protected_file_count": int(len(audit)),
            "git_status_before_run": git_before.splitlines(),
            "git_status_after_run": git_after.splitlines(),
        }


def build_analysis_report(
    *,
    config: MarginalAnalysisConfig,
    end_date: str,
    loo_summary: pd.DataFrame,
    aob_summary: pd.DataFrame,
    period_contribution: pd.DataFrame,
    grouped_metrics: pd.DataFrame,
    group_correlation: pd.DataFrame,
) -> str:
    full_metrics = grouped_metrics[
        (grouped_metrics["period"] == OOS_FULL)
        & (grouped_metrics["strategy_id"] == FULL_11)
    ]
    fixed_metrics = grouped_metrics[
        (grouped_metrics["period"] == OOS_FULL)
        & (grouped_metrics["strategy_id"] == FIXED_4)
    ]
    grouped_full_metrics = grouped_metrics[
        (grouped_metrics["period"] == OOS_FULL)
        & (grouped_metrics["strategy_id"] == GROUPED_8)
    ]
    full_row = full_metrics.iloc[0] if not full_metrics.empty else pd.Series(dtype=object)
    fixed_row = fixed_metrics.iloc[0] if not fixed_metrics.empty else pd.Series(dtype=object)
    grouped_row = (
        grouped_full_metrics.iloc[0]
        if not grouped_full_metrics.empty
        else pd.Series(dtype=object)
    )

    factor_lines = []
    combined = pd.concat([loo_summary, aob_summary], ignore_index=True)
    for row in combined.sort_values(["experiment", "factor"]).to_dict("records"):
        factor_lines.append(
            {
                "实验": "LOO" if row["experiment"] == "leave_one_out" else "Add-back",
                "因子": row["factor"],
                "D10-D1边际": _pct(row.get("d10_d1_annual_return_delta")),
                "D10稳定性": row.get("d10_d1_annual_return_stability", ""),
                "Top20超额边际": _pct(row.get("top20_excess_annual_return_delta")),
                "Top20稳定性": row.get(
                    "top20_excess_annual_return_stability", ""
                ),
                "RankIC边际": _num(row.get("rank_ic_mean_delta")),
            }
        )
    factor_table = pd.DataFrame(factor_lines)

    positive_loo_d10 = _factors_with_label(
        loo_summary, "d10_d1_annual_return_stability", "stable_positive"
    )
    negative_loo_d10 = _factors_with_label(
        loo_summary, "d10_d1_annual_return_stability", "stable_negative"
    )
    positive_loo_top = _factors_with_label(
        loo_summary, "top20_excess_annual_return_stability", "stable_positive"
    )
    negative_loo_top = _factors_with_label(
        loo_summary, "top20_excess_annual_return_stability", "stable_negative"
    )
    positive_aob_d10 = _factors_with_label(
        aob_summary, "d10_d1_annual_return_stability", "stable_positive"
    )
    negative_aob_d10 = _factors_with_label(
        aob_summary, "d10_d1_annual_return_stability", "stable_negative"
    )
    positive_aob_top = _factors_with_label(
        aob_summary, "top20_excess_annual_return_stability", "stable_positive"
    )
    negative_aob_top = _factors_with_label(
        aob_summary, "top20_excess_annual_return_stability", "stable_negative"
    )

    full_vs_fixed_d10 = _difference(
        full_row.get("d10_d1_annual_return"), fixed_row.get("d10_d1_annual_return")
    )
    full_vs_fixed_top = _difference(
        full_row.get("top20_excess_annual_return"),
        fixed_row.get("top20_excess_annual_return"),
    )
    grouped_vs_full_d10 = _difference(
        grouped_row.get("d10_d1_annual_return"),
        full_row.get("d10_d1_annual_return"),
    )
    grouped_vs_full_top = _difference(
        grouped_row.get("top20_excess_annual_return"),
        full_row.get("top20_excess_annual_return"),
    )
    grouped_improves_both = (
        pd.notna(grouped_vs_full_d10)
        and grouped_vs_full_d10 > 0
        and pd.notna(grouped_vs_full_top)
        and grouped_vs_full_top > 0
    )
    grouped_periods = grouped_metrics[grouped_metrics["period"] != OOS_FULL]
    grouped_stable = _grouped_improvement_stability(grouped_periods)
    replacement_supported = grouped_improves_both and grouped_stable

    off_diagonal = group_correlation.to_numpy(dtype=float).copy()
    if off_diagonal.size:
        np.fill_diagonal(off_diagonal, np.nan)
    average_abs_group_corr = (
        float(np.nanmean(np.abs(off_diagonal)))
        if np.isfinite(off_diagonal).any()
        else np.nan
    )

    protocol_table = pd.DataFrame(
        [
            {
                "组合": strategy,
                "D10-D1年化": _pct(row.get("d10_d1_annual_return")),
                "Top20年化超额": _pct(row.get("top20_excess_annual_return")),
                "RankIC均值": _num(row.get("rank_ic_mean")),
                "D10最大回撤": _pct(row.get("d10_d1_max_drawdown")),
                "Top20最大回撤": _pct(row.get("top20_max_drawdown")),
            }
            for strategy, row in (
                (FULL_11, full_row),
                (FIXED_4, fixed_row),
                (GROUPED_8, grouped_row),
            )
        ]
    )
    return f"""# 多因子边际贡献与经济属性分组验证

## 研究协议

- 训练期：2022-01-01 至 2024-12-31；样本外期：2025-01-01 至 {end_date}。
- 因子与方向来自冻结源配置 `{config.source_factor_config.name}`，未使用样本外收益调整因子或权重。
- D10-D1 为传统持有期毛收益；Top20 扣除单边 {config.transaction_cost:.2%} 换手成本后与 `{config.benchmark}` 比较。
- 边际贡献为“包含该因子的组合减不包含该因子的对应基准”，属于基准依赖的局部贡献，不具备 Shapley 可加性。

## 样本外组合对比

{_markdown_table(protocol_table)}

## 1. 因子的正负边际贡献

- LOO 稳定正向 D10-D1：{_join_factors(positive_loo_d10)}；稳定负向：{_join_factors(negative_loo_d10)}。
- LOO 稳定正向 Top20 超额：{_join_factors(positive_loo_top)}；稳定负向：{_join_factors(negative_loo_top)}。
- Add-back 稳定正向 D10-D1：{_join_factors(positive_aob_d10)}；稳定负向：{_join_factors(negative_aob_d10)}。
- Add-back 稳定正向 Top20 超额：{_join_factors(positive_aob_top)}；稳定负向：{_join_factors(negative_aob_top)}。
- 两个主要指标独立判断；一个因子在两个指标上结论冲突时不合并为单一评分。

{_markdown_table(factor_table)}

## 2. 原始 11 因子为何优于固定 4 因子

- 原始 11 因子相对固定 4 因子的 D10-D1 年化差为 {_pct(full_vs_fixed_d10)}，Top20 年化超额差为 {_pct(full_vs_fixed_top)}。
- 被筛除因子的 Add-back 与 LOO 结果表明，弱单因子 RankIC 不等于没有组合价值；部分因子通过截面排序和风格分散改善至少一个主要指标。
- 训练期 8 个经济组之间的平均绝对相关性为 {_num(average_abs_group_corr)}。该结果用于验证分散程度，但不把相关性本身解释为收益因果。

## 3. 经济属性分组是否同时改善两个主要指标

- 相对原始 11 因子，分组组合的 D10-D1 变化为 {_pct(grouped_vs_full_d10)}，Top20 年化超额变化为 {_pct(grouped_vs_full_top)}。
- 结论：{"两个主要指标均改善" if grouped_improves_both else "没有同时改善两个主要指标"}。

## 4. 跨样本外子区间稳定性

- 稳定标签要求全样本外贡献同号，且 2025H1、2025H2、2026YTD 中至少两个子区间同号。
- 分组组合相对原始 11 因子在两个主要指标上的子区间改善{"稳定" if grouped_stable else "不稳定"}。
- 完整逐月与分期结果见 `monthly_marginal_contribution.csv` 和 `period_marginal_contribution.csv`，分期仅用于归因，未用于修改规则。

## 5. 是否有充分证据替换原始 11 因子等权组合

**{"有" if replacement_supported else "没有"}充分证据替换原始 11 因子等权组合。**

该判断要求候选分组组合在全样本外同时改善 D10-D1 和 Top20 超额，并在多个预定义子区间保持方向稳定。允许最终结论为没有稳定改进方案，不根据本次样本外结果继续调参。
"""


def snapshot_protected_files(workspace_root: Path, output_dir: Path) -> dict[str, tuple[int, int]]:
    workspace_root = workspace_root.resolve()
    output_dir = output_dir.resolve()
    roots = [workspace_root / "database", workspace_root / "factor_research"]
    snapshot: dict[str, tuple[int, int]] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if _is_relative_to(resolved, output_dir):
                continue
            relative_parts = resolved.relative_to(workspace_root).parts
            if any(part in {"__pycache__", ".pytest_cache"} for part in relative_parts):
                continue
            stat = resolved.stat()
            snapshot[resolved.relative_to(workspace_root).as_posix()] = (
                int(stat.st_size),
                int(stat.st_mtime_ns),
            )
    return snapshot


def compare_protected_snapshots(
    before: Mapping[str, tuple[int, int]],
    after: Mapping[str, tuple[int, int]],
) -> pd.DataFrame:
    rows = []
    for path in sorted(set(before).union(after)):
        before_value = before.get(path)
        after_value = after.get(path)
        rows.append(
            {
                "path": path,
                "before_exists": before_value is not None,
                "after_exists": after_value is not None,
                "before_size": before_value[0] if before_value else pd.NA,
                "after_size": after_value[0] if after_value else pd.NA,
                "before_mtime_ns": before_value[1] if before_value else pd.NA,
                "after_mtime_ns": after_value[1] if after_value else pd.NA,
                "unchanged": before_value == after_value,
            }
        )
    return pd.DataFrame(rows)


def _write_images(
    images: Path,
    loo_summary: pd.DataFrame,
    aob_summary: pd.DataFrame,
    period_contribution: pd.DataFrame,
    group_correlation: pd.DataFrame,
    grouped_metrics: pd.DataFrame,
) -> dict[str, Path]:
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter

    plt.style.use("seaborn-v0_8-whitegrid")
    paths: dict[str, Path] = {}
    for name, frame, title in (
        ("leave_one_out", loo_summary, "Leave-one-out factor contribution"),
        ("add_one_back", aob_summary, "Add-one-back factor contribution"),
    ):
        if frame.empty:
            continue
        path = images / f"{name}_primary_metrics.png"
        table = frame.sort_values("d10_d1_annual_return_delta")
        y = np.arange(len(table))
        fig, axes = plt.subplots(1, 2, figsize=(14, max(4.5, 0.45 * len(table))))
        axes[0].barh(y, table["d10_d1_annual_return_delta"], color="#4c78a8")
        axes[1].barh(y, table["top20_excess_annual_return_delta"], color="#f58518")
        for ax, metric_title in zip(axes, ("D10-D1 annual contribution", "Top20 excess annual contribution")):
            ax.axvline(0, color="#333333", linewidth=0.8)
            ax.set_yticks(y, table["factor"])
            ax.xaxis.set_major_formatter(PercentFormatter(1.0))
            ax.set_title(metric_title)
        fig.suptitle(title)
        fig.tight_layout()
        fig.savefig(path, dpi=170)
        plt.close(fig)
        paths[f"{name}_image"] = path

    if not period_contribution.empty:
        path = images / "subperiod_stability.png"
        pivot = period_contribution.pivot_table(
            index=["experiment", "factor"],
            columns="period",
            values="d10_d1_annual_return_delta",
            aggfunc="first",
        )
        fig, ax = plt.subplots(figsize=(10, max(5, len(pivot) * 0.35)))
        image = ax.imshow(pivot.to_numpy(dtype=float), aspect="auto", cmap="RdYlGn")
        ax.set_xticks(range(len(pivot.columns)), pivot.columns)
        ax.set_yticks(
            range(len(pivot.index)),
            [f"{experiment}:{factor}" for experiment, factor in pivot.index],
        )
        ax.set_title("D10-D1 contribution by predefined OOS subperiod")
        fig.colorbar(image, ax=ax, format=PercentFormatter(1.0))
        fig.tight_layout()
        fig.savefig(path, dpi=170)
        plt.close(fig)
        paths["subperiod_stability_image"] = path

    if not group_correlation.empty:
        path = images / "group_correlation_heatmap.png"
        fig, ax = plt.subplots(figsize=(8, 7))
        image = ax.imshow(
            group_correlation.to_numpy(dtype=float),
            vmin=-1,
            vmax=1,
            cmap="coolwarm",
        )
        ax.set_xticks(
            range(len(group_correlation.columns)),
            group_correlation.columns,
            rotation=45,
            ha="right",
        )
        ax.set_yticks(range(len(group_correlation.index)), group_correlation.index)
        ax.set_title("Training-period mean monthly Spearman correlation")
        fig.colorbar(image, ax=ax)
        fig.tight_layout()
        fig.savefig(path, dpi=170)
        plt.close(fig)
        paths["group_correlation_image"] = path

    full = grouped_metrics[grouped_metrics["period"] == OOS_FULL]
    if not full.empty:
        path = images / "grouped_composite_comparison.png"
        labels = [FULL_11, FIXED_4, GROUPED_8]
        table = full.set_index("strategy_id").reindex(labels)
        x = np.arange(len(labels))
        width = 0.36
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.bar(
            x - width / 2,
            table["d10_d1_annual_return"],
            width,
            label="D10-D1",
        )
        ax.bar(
            x + width / 2,
            table["top20_excess_annual_return"],
            width,
            label="Top20 excess",
        )
        ax.axhline(0, color="#333333", linewidth=0.8)
        ax.set_xticks(x, labels, rotation=15)
        ax.yaxis.set_major_formatter(PercentFormatter(1.0))
        ax.set_title("OOS grouped composite comparison")
        ax.legend(frameon=False)
        fig.tight_layout()
        fig.savefig(path, dpi=170)
        plt.close(fig)
        paths["grouped_comparison_image"] = path
    return paths


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Run frozen factor marginal contribution and economic-group analysis."
    )
    parser.add_argument(
        "--config",
        default=str(root / "configs" / "factor_marginal_analysis.yaml"),
    )
    parser.add_argument("--end", default=None, help="Optional data end date, YYYY-MM-DD.")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory; defaults to outputs/factor_marginal_analysis/.",
    )
    parser.add_argument("--database-path", default=None)
    parser.add_argument("--paths-config", default=None)
    return parser


def run_from_args(argv: list[str] | None = None) -> dict[str, Path]:
    args = build_parser().parse_args(argv)
    config = load_analysis_config(args.config)
    paths = load_paths(args.paths_config)
    db_path = (
        Path(args.database_path).resolve()
        if args.database_path
        else paths.database_path
    )
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else paths.output_root / "factor_marginal_analysis"
    )
    runner = FactorMarginalAnalysisRunner(
        project_root=paths.database_root,
        db_path=db_path,
        output_dir=output_dir,
        config=config,
        end=args.end,
    )
    return runner.run()


def _compound(values: Iterable[object]) -> float:
    series = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if series.empty:
        return np.nan
    return float((1.0 + series).prod() - 1.0)


def _periodic_annual_return(values: Iterable[object]) -> float:
    series = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if series.empty:
        return np.nan
    return float((1.0 + series).prod() ** (12.0 / len(series)) - 1.0)


def _daily_annual_return(values: Iterable[object]) -> float:
    series = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if series.empty:
        return np.nan
    return float((1.0 + series).prod() ** (252.0 / len(series)) - 1.0)


def _max_drawdown(values: Iterable[object]) -> float:
    series = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if series.empty:
        return np.nan
    nav = (1.0 + series).cumprod()
    return float((nav / nav.cummax() - 1.0).min())


def _mean_or_nan(values: Iterable[object]) -> float:
    series = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    return float(series.mean()) if not series.empty else np.nan


def _coverage_diagnostics(composite_scores: pd.DataFrame) -> pd.DataFrame:
    if composite_scores.empty:
        return pd.DataFrame()
    return (
        composite_scores.assign(valid_score=composite_scores["score"].notna())
        .groupby(["factor", "trade_date"], as_index=False)
        .agg(
            valid_stock_count=("valid_score", "sum"),
            total_stock_count=("ts_code", "nunique"),
            average_available_component_count=("available_component_count", "mean"),
            configured_component_count=("configured_component_count", "max"),
            available_weight_sum_min=("available_weight_sum", "min"),
            available_weight_sum_max=("available_weight_sum", "max"),
        )
        .sort_values(["factor", "trade_date"])
        .reset_index(drop=True)
    )


def _safe_output_path(output_dir: Path, filename: str) -> Path:
    output_dir = output_dir.resolve()
    path = (output_dir / filename).resolve()
    if not _is_relative_to(path, output_dir):
        raise ValueError(f"output path escapes independent output directory: {path}")
    return path


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _git_status(workspace_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(workspace_root), "status", "--porcelain=v1"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date_with_dashes(value: str | None) -> str | None:
    if value is None:
        return None
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def _markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return ""
    columns = [str(column) for column in frame.columns]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in frame.fillna("").astype(str).itertuples(index=False):
        lines.append(
            "| "
            + " | ".join(str(value).replace("|", "\\|") for value in row)
            + " |"
        )
    return "\n".join(lines)


def _pct(value: object) -> str:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return "" if pd.isna(numeric) else f"{float(numeric):.2%}"


def _num(value: object) -> str:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return "" if pd.isna(numeric) else f"{float(numeric):.4f}"


def _difference(left: object, right: object) -> float:
    values = pd.to_numeric(pd.Series([left, right]), errors="coerce")
    return float(values.iloc[0] - values.iloc[1]) if values.notna().all() else np.nan


def _factors_with_label(frame: pd.DataFrame, column: str, label: str) -> list[str]:
    if frame.empty or column not in frame:
        return []
    return sorted(frame.loc[frame[column] == label, "factor"].astype(str).tolist())


def _join_factors(factors: Sequence[str]) -> str:
    return "、".join(factors) if factors else "无"


def _grouped_improvement_stability(grouped_periods: pd.DataFrame) -> bool:
    if grouped_periods.empty:
        return False
    pivot = grouped_periods.set_index(["period", "strategy_id"])
    positive_counts: dict[str, int] = {}
    for metric in PRIMARY_METRICS:
        count = 0
        for period in grouped_periods["period"].drop_duplicates():
            if (period, GROUPED_8) not in pivot.index or (period, FULL_11) not in pivot.index:
                continue
            grouped_value = pd.to_numeric(
                pd.Series([pivot.loc[(period, GROUPED_8), metric]]), errors="coerce"
            ).iloc[0]
            full_value = pd.to_numeric(
                pd.Series([pivot.loc[(period, FULL_11), metric]]), errors="coerce"
            ).iloc[0]
            if pd.notna(grouped_value) and pd.notna(full_value) and grouped_value > full_value:
                count += 1
        positive_counts[metric] = count
    return all(count >= 2 for count in positive_counts.values())
