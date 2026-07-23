from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb
import numpy as np
import pandas as pd
import yaml

from .factor_backtest import (
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
)
from .multifactor import COMPOSITE_EQUAL, COMPOSITE_ROLLING_RANKIC, build_composite_scores
from .paths import load_paths


CHAMPION_STRATEGY = "Champion_SingleFactor"
BENCHMARK_STRATEGY = "Benchmark_000985_CSI"


@dataclass(frozen=True)
class WalkForwardConfig:
    start: str = "2022-01-01"
    end: str | None = None
    warmup_start: str = "2021-01-01"
    benchmark: str = "000985.CSI"
    transaction_cost: float = 0.001
    train_periods: int = 24
    validation_periods: int = 12
    oos_periods: int = 6
    step_periods: int = 6
    min_train_rankic_periods: int = 18
    min_validation_return_periods: int = 9
    champion_switch_sharpe_margin: float = 0.10
    top_quantile: float = 0.20
    bucket_count: int = 10

    def validate(self) -> None:
        if self.train_periods < 1 or self.validation_periods < 1 or self.oos_periods < 1:
            raise ValueError("train, validation, and OOS periods must be positive")
        if self.step_periods < self.oos_periods:
            raise ValueError("step_periods must be at least oos_periods to prevent overlapping OOS folds")
        if not 0 < self.top_quantile <= 1:
            raise ValueError("top_quantile must be in (0, 1]")
        if self.bucket_count < 2:
            raise ValueError("bucket_count must be at least 2")
        if self.transaction_cost < 0 or self.champion_switch_sharpe_margin < 0:
            raise ValueError("cost and switch margin cannot be negative")


@dataclass(frozen=True)
class Stage1Artifact:
    path: Path
    schema_version: int
    artifact_id: str
    locked_at: str
    source: str
    sha256: str
    factor_directions: dict[str, int]
    candidates: tuple[dict[str, Any], ...]

    @property
    def factor_ids(self) -> list[str]:
        return list(self.factor_directions)


def load_experiment_config(path: str | Path) -> tuple[Stage1Artifact, WalkForwardConfig]:
    config_path = Path(path).resolve()
    payload = config_path.read_bytes()
    raw = yaml.safe_load(payload.decode("utf-8")) or {}
    factor_pool = raw.get("factor_pool") or []
    directions = {str(row["id"]): int(row["direction"]) for row in factor_pool}
    if len(directions) != 11 or any(value not in {-1, 1} for value in directions.values()):
        raise ValueError("stage-one factor_pool must contain 11 unique factors with direction -1 or 1")

    candidates = tuple(raw.get("multifactor_candidates") or [])
    candidate_methods = {str(row.get("id")): str(row.get("method")) for row in candidates}
    expected = {COMPOSITE_EQUAL: "equal", COMPOSITE_ROLLING_RANKIC: "rolling_rankic"}
    if candidate_methods != expected:
        raise ValueError(f"stage-one candidates must be exactly {expected}")

    defaults = raw.get("defaults") or {}
    allowed = set(WalkForwardConfig.__dataclass_fields__)
    config = WalkForwardConfig(**{key: value for key, value in defaults.items() if key in allowed})
    config.validate()
    artifact = Stage1Artifact(
        path=config_path,
        schema_version=int(raw.get("schema_version", 0)),
        artifact_id=str(raw.get("artifact_id", "")),
        locked_at=str(raw.get("locked_at", "")),
        source=str(raw.get("source", "")),
        sha256=hashlib.sha256(payload).hexdigest(),
        factor_directions=directions,
        candidates=candidates,
    )
    return artifact, config


def build_period_calendar(rebalance_calendar: pd.DataFrame) -> pd.DataFrame:
    required = {"signal_date", "exec_date"}
    if required - set(rebalance_calendar.columns):
        raise ValueError("rebalance_calendar must include signal_date and exec_date")
    periods = rebalance_calendar[["signal_date", "exec_date"]].dropna().drop_duplicates().copy()
    periods["signal_date"] = periods["signal_date"].astype(str)
    periods["exec_date"] = periods["exec_date"].astype(str)
    periods = periods.sort_values("signal_date").reset_index(drop=True)
    periods["next_exec_date"] = periods["exec_date"].shift(-1)
    return periods


def build_walk_forward_schedule(
    period_calendar: pd.DataFrame,
    config: WalkForwardConfig,
    end_date: str,
) -> pd.DataFrame:
    config.validate()
    periods = build_period_calendar(period_calendar)
    history_periods = config.train_periods + config.validation_periods
    rows: list[dict[str, Any]] = []

    first_decision: int | None = None
    for idx, period in periods.iterrows():
        eligible = periods[(periods.index < idx) & (periods["next_exec_date"].notna())]
        eligible = eligible[eligible["next_exec_date"].astype(str) <= str(period["signal_date"])]
        if len(eligible) >= history_periods:
            first_decision = int(idx)
            break
    if first_decision is None:
        return pd.DataFrame(columns=_schedule_columns())

    fold_number = 1
    decision_idx = first_decision
    while decision_idx < len(periods):
        decision_signal = str(periods.loc[decision_idx, "signal_date"])
        eligible = periods[(periods.index < decision_idx) & periods["next_exec_date"].notna()].copy()
        eligible = eligible[eligible["next_exec_date"].astype(str) <= decision_signal].tail(history_periods)
        if len(eligible) < history_periods:
            decision_idx += config.step_periods
            continue

        train = eligible.head(config.train_periods)
        validation = eligible.tail(config.validation_periods)
        requested_oos = periods.iloc[decision_idx : decision_idx + config.oos_periods].copy()
        completed_oos = requested_oos[
            requested_oos["next_exec_date"].notna()
            & (requested_oos["next_exec_date"].astype(str) <= str(end_date))
        ]
        status = "complete" if len(completed_oos) == config.oos_periods else "incomplete"
        oos = requested_oos if status == "complete" else completed_oos
        embargo = periods.iloc[decision_idx - 1] if decision_idx > 0 else None
        rows.append(
            {
                "fold_id": f"F{fold_number:03d}",
                "status": status,
                "decision_signal_date": decision_signal,
                "train_start": str(train.iloc[0]["signal_date"]),
                "train_end": str(train.iloc[-1]["signal_date"]),
                "validation_start": str(validation.iloc[0]["signal_date"]),
                "validation_end": str(validation.iloc[-1]["signal_date"]),
                "embargo_signal_date": str(embargo["signal_date"]) if embargo is not None else pd.NA,
                "oos_start": str(oos.iloc[0]["signal_date"]) if not oos.empty else pd.NA,
                "oos_end": str(oos.iloc[-1]["signal_date"]) if not oos.empty else pd.NA,
                "oos_completed_periods": int(len(completed_oos)),
                "train_signal_dates": tuple(train["signal_date"].astype(str)),
                "validation_signal_dates": tuple(validation["signal_date"].astype(str)),
                "oos_signal_dates": tuple(oos["signal_date"].astype(str)),
            }
        )
        fold_number += 1
        if status == "incomplete":
            break
        decision_idx += config.step_periods
    return pd.DataFrame(rows, columns=_schedule_columns())


def build_long_short_period_returns(
    scores: pd.DataFrame,
    forward_returns: pd.DataFrame,
    transaction_cost: float,
    bucket_count: int = 10,
    score_col: str = "neutralized_score",
) -> pd.DataFrame:
    deciles = assign_score_deciles(scores, bucket_count=bucket_count, score_col=score_col)
    period_returns = compute_decile_returns(deciles, forward_returns)
    if deciles.empty or period_returns.empty:
        return pd.DataFrame(columns=_long_short_columns())

    return_pivot = period_returns.pivot_table(
        index=["factor", "trade_date"], columns="decile", values="average_forward_return", aggfunc="mean"
    )
    rows: list[dict[str, Any]] = []
    for factor, group in deciles.groupby("factor", sort=True):
        previous_top: pd.Series | None = None
        previous_bottom: pd.Series | None = None
        for trade_date, cross_section in group.groupby("trade_date", sort=True):
            top = _equal_bucket_weights(cross_section, bucket_count)
            bottom = _equal_bucket_weights(cross_section, 1)
            top_turnover = one_way_turnover(previous_top, top)
            bottom_turnover = one_way_turnover(previous_bottom, bottom)
            key = (factor, trade_date)
            top_return = return_pivot.loc[key, bucket_count] if key in return_pivot.index and bucket_count in return_pivot else pd.NA
            bottom_return = return_pivot.loc[key, 1] if key in return_pivot.index and 1 in return_pivot else pd.NA
            gross = (
                float(top_return) - float(bottom_return)
                if pd.notna(top_return) and pd.notna(bottom_return)
                else pd.NA
            )
            cost = transaction_cost * (top_turnover + bottom_turnover)
            rows.append(
                {
                    "factor": factor,
                    "trade_date": str(trade_date),
                    "d10_return": top_return,
                    "d1_return": bottom_return,
                    "gross_long_short_return": gross,
                    "top_turnover": top_turnover,
                    "bottom_turnover": bottom_turnover,
                    "two_leg_turnover": top_turnover + bottom_turnover,
                    "transaction_cost": cost,
                    "net_long_short_return": float(gross) - cost if pd.notna(gross) else pd.NA,
                }
            )
            previous_top = top
            previous_bottom = bottom
    return pd.DataFrame(rows, columns=_long_short_columns()).sort_values(["factor", "trade_date"]).reset_index(drop=True)


def select_champion(
    factor_ids: Iterable[str],
    rank_ic_by_period: pd.DataFrame,
    long_short_period_returns: pd.DataFrame,
    train_dates: Iterable[str],
    validation_dates: Iterable[str],
    incumbent: str | None,
    config: WalkForwardConfig,
) -> tuple[pd.DataFrame, str | None, str]:
    train_set = set(map(str, train_dates))
    validation_set = set(map(str, validation_dates))
    rows: list[dict[str, Any]] = []
    for factor in factor_ids:
        factor_ic = rank_ic_by_period[rank_ic_by_period["factor"] == factor].copy()
        train_ic = pd.to_numeric(
            factor_ic[factor_ic["trade_date"].astype(str).isin(train_set)]["rank_ic"], errors="coerce"
        ).dropna()
        validation_ic = pd.to_numeric(
            factor_ic[factor_ic["trade_date"].astype(str).isin(validation_set)]["rank_ic"], errors="coerce"
        ).dropna()
        factor_returns = long_short_period_returns[
            (long_short_period_returns["factor"] == factor)
            & long_short_period_returns["trade_date"].astype(str).isin(validation_set)
        ]
        validation_returns = pd.to_numeric(factor_returns["net_long_short_return"], errors="coerce").dropna()
        sharpe = _monthly_sharpe(validation_returns)
        average_turnover = pd.to_numeric(factor_returns["two_leg_turnover"], errors="coerce").mean()
        eligible = (
            len(train_ic) >= config.min_train_rankic_periods
            and len(validation_returns) >= config.min_validation_return_periods
            and pd.notna(sharpe)
            and math.isfinite(float(sharpe))
        )
        reasons = []
        if len(train_ic) < config.min_train_rankic_periods:
            reasons.append("insufficient_train_rankic")
        if len(validation_returns) < config.min_validation_return_periods:
            reasons.append("insufficient_validation_returns")
        if pd.isna(sharpe) or not math.isfinite(float(sharpe)):
            reasons.append("invalid_validation_sharpe")
        rows.append(
            {
                "factor_id": factor,
                "train_rankic_periods": int(len(train_ic)),
                "train_rankic_mean": train_ic.mean() if len(train_ic) else pd.NA,
                "validation_rankic_periods": int(len(validation_ic)),
                "validation_rankic_mean": validation_ic.mean() if len(validation_ic) else pd.NA,
                "validation_return_periods": int(len(validation_returns)),
                "validation_net_long_short_return_mean": validation_returns.mean() if len(validation_returns) else pd.NA,
                "validation_sharpe": sharpe,
                "validation_average_two_leg_turnover": average_turnover,
                "is_eligible": bool(eligible),
                "eligibility_reason": "eligible" if eligible else "|".join(reasons),
            }
        )

    log = pd.DataFrame(rows)
    eligible = log[log["is_eligible"]].copy()
    if eligible.empty:
        log["selection_rank"] = pd.NA
        log["selected"] = False
        log["decision_reason"] = "no_eligible_factor"
        return log, None, "no_eligible_factor"

    eligible = eligible.sort_values(
        ["validation_sharpe", "validation_average_two_leg_turnover", "validation_rankic_mean", "factor_id"],
        ascending=[False, True, False, True],
        na_position="last",
    ).reset_index(drop=True)
    rank_map = {factor: idx + 1 for idx, factor in enumerate(eligible["factor_id"])}
    best = str(eligible.iloc[0]["factor_id"])
    eligible_ids = set(eligible["factor_id"].astype(str))

    if incumbent is None:
        champion = best
        reason = "initial_champion"
    elif incumbent not in eligible_ids:
        champion = best
        reason = "incumbent_ineligible"
    elif best == incumbent:
        champion = incumbent
        reason = "incumbent_remains_best"
    else:
        best_sharpe = float(eligible.loc[eligible["factor_id"] == best, "validation_sharpe"].iloc[0])
        incumbent_sharpe = float(eligible.loc[eligible["factor_id"] == incumbent, "validation_sharpe"].iloc[0])
        if best_sharpe >= incumbent_sharpe + config.champion_switch_sharpe_margin:
            champion = best
            reason = "challenger_margin_met"
        else:
            champion = incumbent
            reason = "incumbent_retained_by_margin"

    log["selection_rank"] = log["factor_id"].map(rank_map).astype("Int64")
    log["selected"] = log["factor_id"].eq(champion)
    log["decision_reason"] = np.where(log["selected"], reason, "not_selected")
    return log.sort_values(["selection_rank", "factor_id"], na_position="last").reset_index(drop=True), champion, reason


def build_point_in_time_composites(
    scores: pd.DataFrame,
    rank_ic_by_period: pd.DataFrame,
    period_calendar: pd.DataFrame,
    factor_ids: Iterable[str],
    rankic_window: int = 12,
    rankic_min_periods: int = 6,
    min_factor_count: int = 6,
    score_col: str = "neutralized_score",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    periods = build_period_calendar(period_calendar)
    factor_set = set(factor_ids)
    base = scores[scores["factor"].isin(factor_set)].copy()
    score_frames: list[pd.DataFrame] = []
    weight_frames: list[pd.DataFrame] = []
    for signal_date in sorted(base["trade_date"].astype(str).unique()):
        eligible_dates = periods[
            periods["next_exec_date"].notna() & (periods["next_exec_date"].astype(str) <= signal_date)
        ]
        eligible_signal_dates = set(eligible_dates["signal_date"].astype(str))
        history = rank_ic_by_period[rank_ic_by_period["trade_date"].astype(str).isin(eligible_signal_dates)]
        current = base[base["trade_date"].astype(str) == signal_date]
        composite, weights = build_composite_scores(
            current,
            rank_ic_by_period=history,
            score_col=score_col,
            rankic_window=rankic_window,
            rankic_min_periods=rankic_min_periods,
            min_factor_count=min_factor_count,
        )
        as_of = eligible_dates["next_exec_date"].astype(str).max() if not eligible_dates.empty else pd.NA
        weights["weight_as_of_date"] = as_of
        score_frames.append(composite)
        weight_frames.append(weights)
    empty_scores = pd.DataFrame(columns=["factor", "trade_date", "ts_code", "score"])
    empty_weights = pd.DataFrame(columns=["trade_date", "composite_factor", "source_factor", "weight"])
    return (
        pd.concat(score_frames, ignore_index=True) if score_frames else empty_scores,
        pd.concat(weight_frames, ignore_index=True) if weight_frames else empty_weights,
    )


class WalkForwardChampionRunner:
    def __init__(
        self,
        project_root: Path,
        db_path: Path,
        output_dir: Path,
        artifact: Stage1Artifact,
        config: WalkForwardConfig,
    ) -> None:
        config.validate()
        self.project_root = Path(project_root)
        self.db_path = Path(db_path)
        self.output_dir = Path(output_dir)
        self.artifact = artifact
        self.config = config
        self._research = FactorResearchRunner(
            project_root=self.project_root,
            db_path=self.db_path,
            output_dir=self.output_dir,
            start=config.start,
            end=config.end,
            warmup_start=config.warmup_start,
            benchmark=config.benchmark,
            transaction_cost=config.transaction_cost,
        )

    def run(self) -> dict[str, Path]:
        tables = self.output_dir / "tables"
        images = self.output_dir / "images"
        exports = self.output_dir / "exports"
        for directory in (self.output_dir, tables, images, exports):
            directory.mkdir(parents=True, exist_ok=True)

        with duckdb.connect(str(self.db_path), read_only=True) as con:
            end_date = yyyymmdd(self.config.end) if self.config.end else self._research._latest_trade_date(con)
            all_trade_dates = self._research._load_trade_dates(con, yyyymmdd(self.config.warmup_start), end_date)
            calendar = build_rebalance_calendar(all_trade_dates, yyyymmdd(self.config.start), end_date)
            signal_dates = calendar["signal_date"].drop_duplicates().reset_index(drop=True)
            con.register("signal_dates", pd.DataFrame({"trade_date": signal_dates}))
            raw_factors = self._research._load_factor_panel(con, signal_dates, end_date)
            execution_universe = self._research._load_execution_universe(con, calendar)
            returns = self._research._load_returns(con, yyyymmdd(self.config.start), end_date)
            suspensions = self._research._load_suspensions(con, yyyymmdd(self.config.start), end_date)
            benchmark_returns = self._research._load_benchmark(con, yyyymmdd(self.config.warmup_start), end_date)
            exposures = self._research._load_exposures(con, signal_dates)

        specs = tuple(
            FactorSpec(name=factor, category="stage1", chinese_name=factor, formula="stage1_locked", direction=direction)
            for factor, direction in self.artifact.factor_directions.items()
        )
        scores = normalize_factor_panel(raw_factors[raw_factors["factor"].isin(self.artifact.factor_ids)], specs=specs)
        neutralized = neutralize_scores(scores, exposures)
        tradable_scores = filter_scores_for_execution_universe(neutralized, calendar, execution_universe)
        forward_returns = build_forward_period_returns(
            returns,
            calendar,
            end_date,
            ts_codes=tradable_scores["ts_code"].dropna().unique(),
            suspensions=suspensions,
        )
        rank_ic = compute_ic_by_period(tradable_scores, forward_returns, score_col="neutralized_score")
        candidate_params = _candidate_parameters(self.artifact)
        composite_scores, composite_weights = build_point_in_time_composites(
            tradable_scores,
            rank_ic,
            calendar,
            self.artifact.factor_ids,
            rankic_window=int(candidate_params["rankic_window"]),
            rankic_min_periods=int(candidate_params["rankic_min_periods"]),
            min_factor_count=int(candidate_params["min_factor_count"]),
        )
        composite_ic = compute_ic_by_period(composite_scores, forward_returns, score_col="score")
        factor_long_short = build_long_short_period_returns(
            tradable_scores,
            forward_returns,
            transaction_cost=self.config.transaction_cost,
            bucket_count=self.config.bucket_count,
        )
        schedule = build_walk_forward_schedule(calendar, self.config, end_date)
        selections, champions = self._select_folds(schedule, rank_ic, factor_long_short)
        run_id = _run_id(self.artifact.sha256, self.config, end_date)
        schedule_output = _serialize_schedule(schedule, run_id)
        selection_output = selections.copy()
        if not selection_output.empty:
            selection_output.insert(0, "run_id", run_id)

        strategy_scores, factor_weights, signal_to_fold = self._build_oos_inputs(
            schedule, champions, tradable_scores, composite_scores, composite_weights, run_id
        )
        oos_daily = build_oos_daily_returns(
            strategy_scores,
            calendar,
            returns,
            suspensions,
            benchmark_returns,
            signal_to_fold,
            champions,
            run_id,
            end_date,
            self.config,
        )
        strategy_ic = _build_strategy_ic(rank_ic, composite_ic, signal_to_fold, champions)
        metrics = compute_oos_metrics(oos_daily, strategy_ic)
        manifest = self._manifest(run_id, end_date)

        paths = {
            "manifest": self.output_dir / "run_manifest.json",
            "report": self.output_dir / "report.md",
            "fold_schedule": tables / "fold_schedule.csv",
            "selection_log": tables / "selection_log.csv",
            "factor_weights": tables / "factor_weights.csv",
            "oos_daily_returns": tables / "oos_daily_returns.csv",
            "oos_metrics": tables / "oos_metrics.csv",
            "oos_nav_image": images / "oos_nav.png",
            "fold_comparison_image": images / "fold_comparison.png",
            "champion_history_image": images / "champion_history.png",
            "export": exports / "walk_forward_champion.zip",
        }
        paths["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        for key, frame in {
            "fold_schedule": schedule_output,
            "selection_log": selection_output,
            "factor_weights": factor_weights,
            "oos_daily_returns": oos_daily,
            "oos_metrics": metrics,
        }.items():
            frame.to_csv(paths[key], index=False, encoding="utf-8-sig")
        self._write_report(paths["report"], schedule_output, selection_output, metrics, manifest)
        _write_images(paths, oos_daily, metrics, selection_output)
        _write_export(paths["export"], self.output_dir)
        return paths

    def _select_folds(
        self,
        schedule: pd.DataFrame,
        rank_ic: pd.DataFrame,
        factor_long_short: pd.DataFrame,
    ) -> tuple[pd.DataFrame, dict[str, str]]:
        logs: list[pd.DataFrame] = []
        champions: dict[str, str] = {}
        incumbent: str | None = None
        for row in schedule.itertuples(index=False):
            if row.status != "complete":
                continue
            log, champion, fold_reason = select_champion(
                self.artifact.factor_ids,
                rank_ic,
                factor_long_short,
                row.train_signal_dates,
                row.validation_signal_dates,
                incumbent,
                self.config,
            )
            log.insert(0, "fold_id", row.fold_id)
            log.insert(1, "decision_signal_date", row.decision_signal_date)
            log["incumbent_before"] = incumbent
            log["fold_decision_reason"] = fold_reason
            log["direction"] = log["factor_id"].map(self.artifact.factor_directions)
            logs.append(log)
            if champion is not None:
                champions[row.fold_id] = champion
                incumbent = champion
        return (pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()), champions

    def _build_oos_inputs(
        self,
        schedule: pd.DataFrame,
        champions: dict[str, str],
        factor_scores: pd.DataFrame,
        composite_scores: pd.DataFrame,
        composite_weights: pd.DataFrame,
        run_id: str,
    ) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, str]]:
        score_frames: list[pd.DataFrame] = []
        weight_frames: list[pd.DataFrame] = []
        signal_to_fold: dict[str, str] = {}
        candidate_ids = {COMPOSITE_EQUAL, COMPOSITE_ROLLING_RANKIC}
        for row in schedule.itertuples(index=False):
            if row.status != "complete" or row.fold_id not in champions:
                continue
            oos_dates = set(row.oos_signal_dates)
            signal_to_fold.update({date: row.fold_id for date in oos_dates})
            champion = champions[row.fold_id]
            champion_scores = factor_scores[
                (factor_scores["factor"] == champion) & factor_scores["trade_date"].astype(str).isin(oos_dates)
            ][["trade_date", "ts_code", "neutralized_score"]].rename(columns={"neutralized_score": "score"})
            champion_scores["factor"] = CHAMPION_STRATEGY
            score_frames.append(champion_scores[["factor", "trade_date", "ts_code", "score"]])
            current_composites = composite_scores[
                composite_scores["factor"].isin(candidate_ids)
                & composite_scores["trade_date"].astype(str).isin(oos_dates)
            ]
            score_frames.append(current_composites[["factor", "trade_date", "ts_code", "score"]])

            for signal_date in sorted(oos_dates):
                weight_frames.append(
                    pd.DataFrame(
                        [
                            {
                                "run_id": run_id,
                                "fold_id": row.fold_id,
                                "signal_date": signal_date,
                                "strategy_id": CHAMPION_STRATEGY,
                                "source_factor": champion,
                                "weight": 1.0,
                                "weight_source": "selected_champion",
                                "weight_as_of_date": row.decision_signal_date,
                                "rank_ic_mean": pd.NA,
                                "rank_ic_periods": pd.NA,
                            }
                        ]
                    )
                )
            current_weights = composite_weights[
                composite_weights["trade_date"].astype(str).isin(oos_dates)
                & composite_weights["composite_factor"].isin(candidate_ids)
            ].rename(columns={"trade_date": "signal_date", "composite_factor": "strategy_id"})
            if not current_weights.empty:
                current_weights.insert(0, "run_id", run_id)
                current_weights.insert(1, "fold_id", current_weights["signal_date"].astype(str).map(signal_to_fold))
                weight_frames.append(
                    current_weights[
                        [
                            "run_id",
                            "fold_id",
                            "signal_date",
                            "strategy_id",
                            "source_factor",
                            "weight",
                            "weight_source",
                            "weight_as_of_date",
                            "rank_ic_mean",
                            "rank_ic_periods",
                        ]
                    ]
                )
        scores = pd.concat(score_frames, ignore_index=True) if score_frames else pd.DataFrame(columns=["factor", "trade_date", "ts_code", "score"])
        weights = pd.concat(weight_frames, ignore_index=True) if weight_frames else pd.DataFrame(columns=_factor_weight_columns())
        if not weights.empty:
            weights = weights.sort_values(["fold_id", "signal_date", "strategy_id", "source_factor"]).reset_index(drop=True)
        return scores, weights, signal_to_fold

    def _manifest(self, run_id: str, end_date: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "code_revision": _git_revision(self.output_dir.parent.parent),
            "data_end_date": end_date,
            "database_path": str(self.db_path),
            "database_open_mode": "read_only",
            "stage1_artifact": {
                "path": str(self.artifact.path),
                "schema_version": self.artifact.schema_version,
                "artifact_id": self.artifact.artifact_id,
                "locked_at": self.artifact.locked_at,
                "source": self.artifact.source,
                "sha256": self.artifact.sha256,
            },
            "parameters": asdict(self.config),
        }

    def _write_report(
        self,
        path: Path,
        schedule: pd.DataFrame,
        selections: pd.DataFrame,
        metrics: pd.DataFrame,
        manifest: dict[str, Any],
    ) -> None:
        complete = schedule[schedule["status"] == "complete"] if not schedule.empty else schedule
        champions = (
            selections[selections["selected"]][["fold_id", "factor_id", "fold_decision_reason"]]
            if not selections.empty
            else pd.DataFrame()
        )
        pooled = metrics[metrics["scope"] == "pooled"] if not metrics.empty else metrics
        content = f"""# Walk-Forward Champion vs Multifactor Report

## Protocol

- Run ID: `{manifest['run_id']}`
- Stage-one artifact: `{self.artifact.artifact_id}` (`{self.artifact.sha256}`)
- Window: `{self.config.train_periods}` training + `{self.config.validation_periods}` validation + `{self.config.oos_periods}` OOS periods; step `{self.config.step_periods}`.
- Champion rule: validation net D10-D1 monthly Sharpe; challenger margin `{self.config.champion_switch_sharpe_margin:.2f}`.
- Signal/execution: month-end signal, next-trading-day execution; only completed holding periods are available to decisions.
- Transaction cost: one-way `{self.config.transaction_cost:.2%}` on both long/short legs.
- Benchmark: `{self.config.benchmark}`.

## Completed Folds

{_markdown_table(complete.drop(columns=[c for c in ['train_signal_dates', 'validation_signal_dates', 'oos_signal_dates'] if c in complete]))}

## Champion History

{_markdown_table(champions)}

## Pooled Out-of-Sample Metrics

{_markdown_table(pooled)}

## Files

- Tables: `tables/fold_schedule.csv`, `selection_log.csv`, `factor_weights.csv`, `oos_daily_returns.csv`, `oos_metrics.csv`
- Images: `images/oos_nav.png`, `fold_comparison.png`, `champion_history.png`
- Export: `exports/walk_forward_champion.zip`
"""
        path.write_text(content, encoding="utf-8")


def build_oos_daily_returns(
    strategy_scores: pd.DataFrame,
    rebalance_calendar: pd.DataFrame,
    returns: pd.DataFrame,
    suspensions: pd.DataFrame,
    benchmark_returns: pd.DataFrame,
    signal_to_fold: dict[str, str],
    champions: dict[str, str],
    run_id: str,
    end_date: str,
    config: WalkForwardConfig,
) -> pd.DataFrame:
    if strategy_scores.empty or not signal_to_fold:
        return pd.DataFrame(columns=_daily_output_columns())
    signals = sorted(signal_to_fold)
    full_calendar = build_period_calendar(rebalance_calendar)
    start_idx = int(full_calendar.index[full_calendar["signal_date"] == signals[0]][0])
    end_idx = int(full_calendar.index[full_calendar["signal_date"] == signals[-1]][0])
    backtest_calendar = full_calendar.iloc[start_idx : min(end_idx + 2, len(full_calendar))][["signal_date", "exec_date"]]

    top_weights = select_top_quantile_weights(strategy_scores, quantile=config.top_quantile, score_col="score")
    deciles = assign_score_deciles(strategy_scores, bucket_count=config.bucket_count, score_col="score")
    d10_weights = _bucket_weight_frame(deciles, config.bucket_count)
    d1_weights = _bucket_weight_frame(deciles, 1)
    leg_frames = []
    for leg, weights in (("top20", top_weights), ("d10", d10_weights), ("d1", d1_weights)):
        daily, _turnover = backtest_monthly_top_quantile(
            weights,
            returns,
            backtest_calendar,
            end_date=end_date,
            transaction_cost=0.0,
            suspensions=suspensions,
        )
        if daily.empty:
            continue
        daily = daily.rename(columns={"factor": "strategy_id", "portfolio_return": "gross_return"})
        daily["portfolio_leg"] = leg
        daily["transaction_cost"] = pd.to_numeric(daily["turnover"], errors="coerce").fillna(0.0) * config.transaction_cost
        daily["net_return"] = daily["gross_return"] - daily["transaction_cost"]
        leg_frames.append(daily)
    if not leg_frames:
        return pd.DataFrame(columns=_daily_output_columns())
    legs = pd.concat(leg_frames, ignore_index=True)
    date_to_signal = _date_to_signal_map(returns["trade_date"].astype(str).unique(), backtest_calendar, end_date)
    legs["signal_date"] = legs["trade_date"].astype(str).map(date_to_signal)
    legs = legs[legs["signal_date"].isin(signal_to_fold)].copy()

    d10 = legs[legs["portfolio_leg"] == "d10"].copy()
    d1 = legs[legs["portfolio_leg"] == "d1"].copy()
    merge_cols = ["strategy_id", "trade_date", "signal_date"]
    long_short = d10.merge(d1, on=merge_cols, suffixes=("_d10", "_d1"), how="inner")
    if not long_short.empty:
        combined = pd.DataFrame(
            {
                "strategy_id": long_short["strategy_id"],
                "trade_date": long_short["trade_date"],
                "signal_date": long_short["signal_date"],
                "portfolio_leg": "long_short",
                "gross_return": long_short["gross_return_d10"] - long_short["gross_return_d1"],
                "turnover": long_short["turnover_d10"] + long_short["turnover_d1"],
                "transaction_cost": long_short["transaction_cost_d10"] + long_short["transaction_cost_d1"],
            }
        )
        combined["net_return"] = combined["gross_return"] - combined["transaction_cost"]
        for col in ["missing_return_count", "suspended_return_count", "invalid_missing_return_count", "invalid_missing_weight"]:
            combined[col] = pd.to_numeric(long_short.get(f"{col}_d10", 0), errors="coerce").fillna(0) + pd.to_numeric(
                long_short.get(f"{col}_d1", 0), errors="coerce"
            ).fillna(0)
        legs = pd.concat([legs, combined], ignore_index=True, sort=False)

    benchmark = benchmark_returns.copy()
    benchmark["trade_date"] = benchmark["trade_date"].astype(str)
    legs = legs.merge(benchmark[["trade_date", "benchmark_return"]], on="trade_date", how="left")
    legs["benchmark_return"] = pd.to_numeric(legs["benchmark_return"], errors="coerce").fillna(0.0)
    legs["excess_return"] = np.where(
        legs["portfolio_leg"] == "long_short",
        legs["net_return"],
        legs["net_return"] - legs["benchmark_return"],
    )
    legs["run_id"] = run_id
    legs["fold_id"] = legs["signal_date"].map(signal_to_fold)
    legs["strategy_type"] = np.where(legs["strategy_id"] == CHAMPION_STRATEGY, "champion_single", "multifactor")
    legs["selected_factor"] = legs["fold_id"].map(champions)

    benchmark_dates = legs[["fold_id", "signal_date", "trade_date", "benchmark_return"]].drop_duplicates()
    benchmark_rows = benchmark_dates.assign(
        run_id=run_id,
        strategy_id=BENCHMARK_STRATEGY,
        strategy_type="benchmark",
        selected_factor=pd.NA,
        portfolio_leg="benchmark",
        gross_return=benchmark_dates["benchmark_return"],
        turnover=0.0,
        transaction_cost=0.0,
        net_return=benchmark_dates["benchmark_return"],
        excess_return=0.0,
        missing_return_count=0,
        suspended_return_count=0,
        invalid_missing_return_count=0,
        invalid_missing_weight=0.0,
    )
    result = pd.concat([legs, benchmark_rows], ignore_index=True, sort=False)
    for column in _daily_output_columns():
        if column not in result:
            result[column] = pd.NA
    result = result[_daily_output_columns()].sort_values(
        ["fold_id", "trade_date", "strategy_id", "portfolio_leg"]
    ).reset_index(drop=True)
    if result.duplicated(["run_id", "fold_id", "trade_date", "strategy_id", "portfolio_leg"]).any():
        raise ValueError("duplicate standardized OOS daily-return key")
    return result


def compute_oos_metrics(oos_daily: pd.DataFrame, strategy_ic: pd.DataFrame) -> pd.DataFrame:
    if oos_daily.empty:
        return pd.DataFrame(columns=_metric_columns())
    frames: list[tuple[str, str, pd.DataFrame]] = []
    for fold_id, group in oos_daily.groupby("fold_id", sort=True):
        frames.append(("fold", str(fold_id), group))
    frames.append(("pooled", "ALL", oos_daily))
    rows: list[dict[str, Any]] = []
    for scope, fold_id, frame in frames:
        for (strategy_id, leg), group in frame.groupby(["strategy_id", "portfolio_leg"], sort=True):
            group = group.sort_values("trade_date")
            if leg == "long_short":
                returns = group.groupby("signal_date")["net_return"].apply(_compound).dropna()
                annual_factor = 12.0
                frequency = "monthly"
            else:
                returns = pd.to_numeric(group["net_return"], errors="coerce").dropna()
                annual_factor = 252.0
                frequency = "daily"
            if returns.empty:
                continue
            nav = (1.0 + returns).cumprod()
            annual_return = nav.iloc[-1] ** (annual_factor / len(returns)) - 1.0
            volatility = returns.std(ddof=0) * math.sqrt(annual_factor)
            sharpe = returns.mean() / returns.std(ddof=0) * math.sqrt(annual_factor) if returns.std(ddof=0) > 0 else pd.NA
            drawdown = nav / nav.cummax() - 1.0
            benchmark_daily = group.drop_duplicates("trade_date").set_index("trade_date")["benchmark_return"].astype(float)
            benchmark_nav = (1.0 + benchmark_daily).cumprod()
            benchmark_annual = benchmark_nav.iloc[-1] ** (252.0 / len(benchmark_daily)) - 1.0 if len(benchmark_daily) else pd.NA
            excess_daily = group.drop_duplicates("trade_date").set_index("trade_date")["excess_return"].astype(float)
            information_ratio = (
                excess_daily.mean() / excess_daily.std(ddof=0) * math.sqrt(252.0)
                if excess_daily.std(ddof=0) > 0
                else pd.NA
            )
            ic_filter = strategy_ic[strategy_ic["strategy_id"] == strategy_id]
            if scope == "fold":
                ic_filter = ic_filter[ic_filter["fold_id"] == fold_id]
            rows.append(
                {
                    "run_id": str(group["run_id"].iloc[0]),
                    "scope": scope,
                    "fold_id": fold_id,
                    "strategy_id": strategy_id,
                    "portfolio_leg": leg,
                    "metric_frequency": frequency,
                    "start_date": str(group["trade_date"].min()),
                    "end_date": str(group["trade_date"].max()),
                    "observation_count": int(len(returns)),
                    "annual_return": annual_return,
                    "annual_volatility": volatility,
                    "sharpe": sharpe,
                    "max_drawdown": drawdown.min(),
                    "win_rate": float((returns > 0).mean()),
                    "cumulative_return": nav.iloc[-1] - 1.0,
                    "average_period_turnover": group.groupby("signal_date")["turnover"].sum().mean(),
                    "total_transaction_cost": group["transaction_cost"].sum(),
                    "benchmark_annual_return": 0.0 if leg == "long_short" else benchmark_annual,
                    "excess_annual_return": annual_return if leg == "long_short" else annual_return - float(benchmark_annual),
                    "information_ratio": information_ratio,
                    "rank_ic_mean": pd.to_numeric(ic_filter["rank_ic"], errors="coerce").mean() if not ic_filter.empty else pd.NA,
                    "rank_ic_periods": int(pd.to_numeric(ic_filter["rank_ic"], errors="coerce").notna().sum()),
                }
            )
    return pd.DataFrame(rows, columns=_metric_columns()).sort_values(
        ["scope", "fold_id", "portfolio_leg", "strategy_id"]
    ).reset_index(drop=True)


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Walk-forward champion single-factor vs multifactor comparison.")
    parser.add_argument("--config", default=str(root / "configs" / "walk_forward_champion.yaml"))
    parser.add_argument("--paths-config", default=None, help="Optional standard project paths config.")
    parser.add_argument("--database-path", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--warmup-start", default=None)
    parser.add_argument("--benchmark", default=None)
    parser.add_argument("--transaction-cost", type=float, default=None)
    parser.add_argument("--train-periods", type=int, default=None)
    parser.add_argument("--validation-periods", type=int, default=None)
    parser.add_argument("--oos-periods", type=int, default=None)
    parser.add_argument("--step-periods", type=int, default=None)
    parser.add_argument("--switch-sharpe-margin", type=float, default=None)
    return parser


def run_from_args(argv: list[str] | None = None) -> dict[str, Path]:
    args = build_parser().parse_args(argv)
    artifact, config = load_experiment_config(args.config)
    overrides = {
        "start": args.start,
        "end": args.end,
        "warmup_start": args.warmup_start,
        "benchmark": args.benchmark,
        "transaction_cost": args.transaction_cost,
        "train_periods": args.train_periods,
        "validation_periods": args.validation_periods,
        "oos_periods": args.oos_periods,
        "step_periods": args.step_periods,
        "champion_switch_sharpe_margin": args.switch_sharpe_margin,
    }
    config = replace(config, **{key: value for key, value in overrides.items() if value is not None})
    config.validate()
    paths = load_paths(args.paths_config)
    db_path = Path(args.database_path).resolve() if args.database_path else paths.database_path
    output_dir = Path(args.output_dir).resolve() if args.output_dir else paths.output_root / "walk_forward_champion"
    runner = WalkForwardChampionRunner(paths.database_root, db_path, output_dir, artifact, config)
    return runner.run()


def _candidate_parameters(artifact: Stage1Artifact) -> dict[str, Any]:
    rolling = next(row for row in artifact.candidates if row["id"] == COMPOSITE_ROLLING_RANKIC)
    equal = next(row for row in artifact.candidates if row["id"] == COMPOSITE_EQUAL)
    rolling_params = rolling.get("parameters") or {}
    equal_params = equal.get("parameters") or {}
    if rolling_params.get("negative_rankic") != "clip_zero" or rolling_params.get("fallback") != "equal":
        raise ValueError("unsupported rolling RankIC stage-one policy")
    if int(rolling_params.get("min_factor_count", 0)) != int(equal_params.get("min_factor_count", 0)):
        raise ValueError("accepted baselines must share min_factor_count")
    return rolling_params


def _schedule_columns() -> list[str]:
    return [
        "fold_id",
        "status",
        "decision_signal_date",
        "train_start",
        "train_end",
        "validation_start",
        "validation_end",
        "embargo_signal_date",
        "oos_start",
        "oos_end",
        "oos_completed_periods",
        "train_signal_dates",
        "validation_signal_dates",
        "oos_signal_dates",
    ]


def _serialize_schedule(schedule: pd.DataFrame, run_id: str) -> pd.DataFrame:
    result = schedule.copy()
    for column in ("train_signal_dates", "validation_signal_dates", "oos_signal_dates"):
        if column in result:
            result[column] = result[column].map(lambda values: "|".join(values) if isinstance(values, tuple) else "")
    result.insert(0, "run_id", run_id)
    return result


def _long_short_columns() -> list[str]:
    return [
        "factor",
        "trade_date",
        "d10_return",
        "d1_return",
        "gross_long_short_return",
        "top_turnover",
        "bottom_turnover",
        "two_leg_turnover",
        "transaction_cost",
        "net_long_short_return",
    ]


def _factor_weight_columns() -> list[str]:
    return [
        "run_id",
        "fold_id",
        "signal_date",
        "strategy_id",
        "source_factor",
        "weight",
        "weight_source",
        "weight_as_of_date",
        "rank_ic_mean",
        "rank_ic_periods",
    ]


def _daily_output_columns() -> list[str]:
    return [
        "run_id",
        "fold_id",
        "signal_date",
        "trade_date",
        "strategy_id",
        "strategy_type",
        "selected_factor",
        "portfolio_leg",
        "gross_return",
        "turnover",
        "transaction_cost",
        "net_return",
        "benchmark_return",
        "excess_return",
        "missing_return_count",
        "suspended_return_count",
        "invalid_missing_return_count",
        "invalid_missing_weight",
    ]


def _metric_columns() -> list[str]:
    return [
        "run_id",
        "scope",
        "fold_id",
        "strategy_id",
        "portfolio_leg",
        "metric_frequency",
        "start_date",
        "end_date",
        "observation_count",
        "annual_return",
        "annual_volatility",
        "sharpe",
        "max_drawdown",
        "win_rate",
        "cumulative_return",
        "average_period_turnover",
        "total_transaction_cost",
        "benchmark_annual_return",
        "excess_annual_return",
        "information_ratio",
        "rank_ic_mean",
        "rank_ic_periods",
    ]


def _equal_bucket_weights(group: pd.DataFrame, bucket: int) -> pd.Series:
    selected = group[group["decile"] == bucket]["ts_code"].dropna().astype(str).drop_duplicates()
    if selected.empty:
        return pd.Series(dtype="float64")
    return pd.Series(1.0 / len(selected), index=selected, dtype="float64")


def _bucket_weight_frame(deciles: pd.DataFrame, bucket: int) -> pd.DataFrame:
    selected = deciles[deciles["decile"] == bucket][["factor", "trade_date", "ts_code"]].drop_duplicates().copy()
    if selected.empty:
        return pd.DataFrame(columns=["factor", "trade_date", "ts_code", "weight"])
    selected["holding_count"] = selected.groupby(["factor", "trade_date"])["ts_code"].transform("count")
    selected["weight"] = 1.0 / selected["holding_count"]
    return selected[["factor", "trade_date", "ts_code", "weight"]]


def _monthly_sharpe(returns: pd.Series) -> float | Any:
    series = pd.to_numeric(returns, errors="coerce").dropna()
    if series.empty:
        return pd.NA
    std = series.std(ddof=0)
    return float(series.mean() / std * math.sqrt(12.0)) if std > 0 else pd.NA


def _date_to_signal_map(trade_dates: Iterable[str], calendar: pd.DataFrame, end_date: str) -> dict[str, str]:
    dates = sorted(map(str, trade_dates))
    periods = build_period_calendar(calendar)
    mapping: dict[str, str] = {}
    for row in periods.itertuples(index=False):
        period_end = str(row.next_exec_date) if pd.notna(row.next_exec_date) else str(end_date)
        for date in dates:
            if str(row.exec_date) < date <= period_end:
                mapping[date] = str(row.signal_date)
    return mapping


def _build_strategy_ic(
    factor_ic: pd.DataFrame,
    composite_ic: pd.DataFrame,
    signal_to_fold: dict[str, str],
    champions: dict[str, str],
) -> pd.DataFrame:
    rows = []
    for signal_date, fold_id in signal_to_fold.items():
        champion = champions.get(fold_id)
        current = factor_ic[(factor_ic["factor"] == champion) & (factor_ic["trade_date"].astype(str) == signal_date)].copy()
        if not current.empty:
            current["strategy_id"] = CHAMPION_STRATEGY
            current["fold_id"] = fold_id
            rows.append(current)
        composites = composite_ic[composite_ic["trade_date"].astype(str) == signal_date].copy()
        if not composites.empty:
            composites["strategy_id"] = composites["factor"]
            composites["fold_id"] = fold_id
            rows.append(composites)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["strategy_id", "fold_id", "rank_ic"])


def _compound(values: pd.Series) -> float:
    series = pd.to_numeric(values, errors="coerce").dropna()
    return float((1.0 + series).prod() - 1.0) if len(series) else float("nan")


def _run_id(artifact_hash: str, config: WalkForwardConfig, end_date: str) -> str:
    payload = json.dumps({"artifact": artifact_hash, "config": asdict(config), "end": end_date}, sort_keys=True)
    return "wf_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _git_revision(workdir: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workdir, check=True, capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "_No rows._"
    selected = frame.copy()
    for column in selected.columns:
        if pd.api.types.is_numeric_dtype(selected[column]):
            selected[column] = selected[column].map(lambda value: "" if pd.isna(value) else f"{value:.4f}")
    header = "| " + " | ".join(selected.columns) + " |"
    divider = "| " + " | ".join(["---"] * len(selected.columns)) + " |"
    rows = ["| " + " | ".join(map(str, row)) + " |" for row in selected.itertuples(index=False, name=None)]
    return "\n".join([header, divider, *rows])


def _write_images(paths: dict[str, Path], daily: pd.DataFrame, metrics: pd.DataFrame, selections: pd.DataFrame) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        for key in ("oos_nav_image", "fold_comparison_image", "champion_history_image"):
            paths.pop(key, None)
        return
    plt.style.use("seaborn-v0_8-whitegrid")

    fig, ax = plt.subplots(figsize=(11, 6))
    top = daily[daily["portfolio_leg"].isin(["top20", "benchmark"])].copy()
    for (strategy, leg), group in top.groupby(["strategy_id", "portfolio_leg"], sort=True):
        group = group.sort_values("trade_date")
        ax.plot(pd.to_datetime(group["trade_date"]), (1.0 + group["net_return"].astype(float)).cumprod(), label=strategy)
    ax.set_title("Walk-Forward Out-of-Sample NAV")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(paths["oos_nav_image"], dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 6))
    folds = metrics[(metrics["scope"] == "fold") & (metrics["portfolio_leg"] == "long_short")].copy()
    if not folds.empty:
        pivot = folds.pivot(index="fold_id", columns="strategy_id", values="sharpe")
        pivot.plot(kind="bar", ax=ax)
    ax.set_title("Fold Net D10-D1 Sharpe")
    fig.tight_layout()
    fig.savefig(paths["fold_comparison_image"], dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 4))
    selected = selections[selections["selected"]].drop_duplicates("fold_id") if not selections.empty else selections
    if not selected.empty:
        factors = sorted(selected["factor_id"].unique())
        mapping = {factor: idx for idx, factor in enumerate(factors)}
        ax.plot(selected["fold_id"], selected["factor_id"].map(mapping), marker="o")
        ax.set_yticks(list(mapping.values()), list(mapping.keys()))
    ax.set_title("Champion History")
    fig.tight_layout()
    fig.savefig(paths["champion_history_image"], dpi=150)
    plt.close(fig)


def _write_export(path: Path, output_dir: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(output_dir.rglob("*")):
            if file.is_file() and file != path:
                archive.write(file, file.relative_to(output_dir))
