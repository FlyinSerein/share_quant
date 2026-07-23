from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import factor_research.factor_marginal_analysis as marginal
from factor_research.factor_diagnostics import (
    assign_score_deciles,
    compute_decile_returns,
    compute_long_short_returns,
)
from factor_research.factor_marginal_analysis import (
    AOB_PREFIX,
    FIXED_4,
    FULL_11,
    GROUPED_8,
    LOO_PREFIX,
    OOS_FULL,
    FactorMarginalAnalysisRunner,
    PeriodSpec,
    attach_execution_dates,
    build_equal_composite_scores,
    build_experiment_definitions,
    build_grouped_composite_scores,
    build_marginal_contributions,
    classify_stability,
    load_analysis_config,
)


CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "factor_marginal_analysis.yaml"


def test_frozen_factor_sets_and_experiment_definitions_are_exact() -> None:
    config = load_analysis_config(CONFIG_PATH)
    definitions = build_experiment_definitions(config)
    loo = [definition for definition in definitions if definition.family == "leave_one_out"]
    add = [definition for definition in definitions if definition.family == "add_one_back"]

    assert len(config.factor_ids) == 11
    assert config.fixed_factor_ids == (
        "PE_TTM",
        "Dividend_Yield",
        "Volatility",
        "Main_Net_In",
    )
    assert len(loo) == 11
    assert {definition.factor_id for definition in loo} == set(config.factor_ids)
    assert all(len(definition.factor_ids) == 10 for definition in loo)
    assert all(
        definition.factor_id not in definition.factor_ids for definition in loo
    )
    excluded = set(config.factor_ids) - set(config.fixed_factor_ids)
    assert len(add) == 7
    assert {definition.factor_id for definition in add} == excluded
    assert all(len(definition.factor_ids) == 5 for definition in add)
    assert all(
        set(config.fixed_factor_ids) < set(definition.factor_ids)
        for definition in add
    )
    assert {definition.strategy_id for definition in loo} == {
        f"{LOO_PREFIX}{factor}" for factor in config.factor_ids
    }
    assert {definition.strategy_id for definition in add} == {
        f"{AOB_PREFIX}{factor}" for factor in excluded
    }


def test_frozen_groups_partition_factor_pool_and_coverage_thresholds() -> None:
    config = load_analysis_config(CONFIG_PATH)

    grouped = [
        factor
        for group in config.groups
        for factor in group.factor_ids
    ]
    assert len(config.groups) == 8
    assert len(grouped) == len(set(grouped)) == 11
    assert set(grouped) == set(config.factor_ids)
    assert {
        group.group_id: group.factor_ids for group in config.groups
    } == {
        "Value": ("PE_TTM", "Dividend_Yield"),
        "Quality": ("ROE", "Debt_to_Equity", "Gross_Margin"),
        "Growth": ("Revenue_Growth",),
        "LowVol": ("Volatility",),
        "Momentum": ("Momentum_60D",),
        "FundFlow": ("Main_Net_In",),
        "Liquidity": ("Turnover_20D",),
        "Ownership": ("Holder_Concen",),
    }
    assert {
        count: config.minimum_component_count(count)
        for count in (11, 10, 5, 4, 8)
    } == {11: 6, 10: 6, 5: 3, 4: 3, 8: 5}


def test_equal_composite_renormalizes_missing_factors_and_enforces_threshold() -> None:
    scores = pd.DataFrame(
        [
            {"factor": "F1", "trade_date": "20250131", "ts_code": "A", "neutralized_score": 1.0},
            {"factor": "F2", "trade_date": "20250131", "ts_code": "A", "neutralized_score": 3.0},
            {"factor": "F3", "trade_date": "20250131", "ts_code": "A", "neutralized_score": pd.NA},
            {"factor": "F1", "trade_date": "20250131", "ts_code": "B", "neutralized_score": 8.0},
        ]
    )

    composite = build_equal_composite_scores(
        scores,
        ["F1", "F2", "F3"],
        "C",
        min_factor_count=2,
    ).set_index("ts_code")

    assert composite.loc["A", "score"] == 2.0
    assert composite.loc["A", "available_component_count"] == 2
    assert composite.loc["A", "available_weight_sum"] == 1.0
    assert pd.isna(composite.loc["B", "score"])
    assert composite.loc["B", "available_weight_sum"] == 0.0


def test_grouped_composite_is_equal_within_group_then_equal_across_groups() -> None:
    config = load_analysis_config(CONFIG_PATH)
    groups = config.groups[:2]
    scores = pd.DataFrame(
        [
            {"factor": "PE_TTM", "trade_date": "20250131", "ts_code": "A", "neutralized_score": 1.0},
            {"factor": "Dividend_Yield", "trade_date": "20250131", "ts_code": "A", "neutralized_score": 3.0},
            {"factor": "ROE", "trade_date": "20250131", "ts_code": "A", "neutralized_score": 7.0},
            {"factor": "Debt_to_Equity", "trade_date": "20250131", "ts_code": "A", "neutralized_score": pd.NA},
            {"factor": "Gross_Margin", "trade_date": "20250131", "ts_code": "A", "neutralized_score": 11.0},
        ]
    )

    composite, group_scores = build_grouped_composite_scores(
        scores, groups, GROUPED_8, min_group_count=2
    )
    by_group = group_scores.set_index("factor")

    assert by_group.loc["Value", "score"] == 2.0
    assert by_group.loc["Quality", "score"] == 9.0
    assert composite.loc[0, "score"] == 5.5
    assert composite.loc[0, "available_component_count"] == 2
    assert composite.loc[0, "available_weight_sum"] == 1.0


def test_d10_d1_matches_existing_gross_forward_period_direction() -> None:
    scores = pd.DataFrame(
        [
            {"factor": "C", "trade_date": "20250131", "ts_code": f"S{i}", "score": float(i)}
            for i in range(10)
        ]
    )
    forward = pd.DataFrame(
        [
            {"signal_date": "20250131", "ts_code": f"S{i}", "forward_return": i / 100.0}
            for i in range(10)
        ]
    )

    deciles = assign_score_deciles(scores, bucket_count=10, score_col="score")
    layer_returns = compute_decile_returns(deciles, forward)
    long_short = compute_long_short_returns(layer_returns, bucket_count=10)

    assert long_short.loc[0, "long_short_return"] == 0.09


def test_execution_month_attribution_includes_prior_year_end_signal() -> None:
    frame = pd.DataFrame(
        [{"factor": "C", "score_date": "20241231", "long_short_return": 0.05}]
    )
    calendar = pd.DataFrame(
        [{"signal_date": "20241231", "exec_date": "20250102"}]
    )

    attributed = attach_execution_dates(frame, calendar, "score_date")
    existing = attach_execution_dates(
        pd.DataFrame(
            [
                {
                    "factor": "C",
                    "signal_date": "20241231",
                    "exec_date": "20250102",
                    "turnover": 1.0,
                }
            ]
        ),
        calendar,
        "signal_date",
    )

    assert attributed.loc[0, "attribution_date"] == "20250102"
    assert attributed.loc[0, "attribution_month"] == "2025-01"
    assert existing.loc[0, "attribution_date"] == "20250102"


def test_marginal_sign_and_stability_are_presence_minus_absence() -> None:
    config = load_analysis_config(CONFIG_PATH)
    definitions = [
        definition
        for definition in build_experiment_definitions(config)
        if definition.factor_id == "ROE" and definition.family == "leave_one_out"
    ]
    rows = []
    for period, full_value, without_value in (
        (OOS_FULL, 0.08, 0.02),
        ("2025H1", 0.04, 0.01),
        ("2025H2", 0.03, 0.01),
        ("2026YTD", -0.01, 0.00),
    ):
        for strategy, value in ((FULL_11, full_value), (f"{LOO_PREFIX}ROE", without_value)):
            row = {"period": period, "strategy_id": strategy}
            for metric in marginal.SUMMARY_METRICS:
                row[metric] = value
            rows.append(row)
    metrics = pd.DataFrame(rows)

    contributions = build_marginal_contributions(
        definitions, metrics, [OOS_FULL, "2025H1", "2025H2", "2026YTD"]
    )
    summary = classify_stability(contributions, config)

    assert summary.loc[0, "d10_d1_annual_return_delta"] == 0.06
    assert summary.loc[0, "d10_d1_annual_return_positive_subperiods"] == 2
    assert summary.loc[0, "d10_d1_annual_return_stability"] == "stable_positive"


def test_runner_requests_read_only_database_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_analysis_config(CONFIG_PATH)
    called: dict[str, object] = {}

    class StopAfterConnect(RuntimeError):
        pass

    def fake_connect(path: str, *, read_only: bool) -> object:
        called["path"] = path
        called["read_only"] = read_only
        raise StopAfterConnect

    monkeypatch.setattr(marginal.duckdb, "connect", fake_connect)
    runner = FactorMarginalAnalysisRunner(
        project_root=tmp_path / "database",
        db_path=tmp_path / "database" / "data" / "dummy.duckdb",
        output_dir=tmp_path / "factor_research" / "outputs" / "factor_marginal_analysis",
        config=config,
    )

    with pytest.raises(StopAfterConnect):
        runner.run()

    assert called["read_only"] is True
