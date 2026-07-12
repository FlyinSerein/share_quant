from __future__ import annotations

import math
import tempfile
from pathlib import Path

import duckdb
import pandas as pd

from factor_research.factor_backtest import (
    FactorResearchRunner,
    FactorSpec,
    backtest_monthly_top_quantile,
    build_rebalance_calendar,
    filter_scores_for_execution_universe,
    normalize_factor_panel,
    one_way_turnover,
    select_top_quantile_weights,
)
from factor_research.factor_diagnostics import (
    FactorDiagnosticsRunner,
    assign_score_deciles,
    build_forward_period_returns,
    compute_decile_returns,
    compute_ic_by_period,
    compute_long_short_returns,
    neutralize_scores,
)
from factor_research.multifactor import (
    COMPOSITE_EQUAL,
    COMPOSITE_ROLLING_RANKIC,
    assign_score_layers,
    build_composite_scores,
    build_layer_weights,
    build_multifactor_results,
    build_parser as build_multifactor_parser,
)
from factor_research.paths import load_paths


def test_default_paths_resolve_to_sibling_database_and_stage_outputs() -> None:
    paths = load_paths()

    assert paths.database_path.name == "share_quant.duckdb"
    assert paths.database_root.name == "database"
    assert paths.output_root.name == "outputs"


def test_runner_accepts_parallel_neutralized_output(tmp_path: Path) -> None:
    neutralized = tmp_path / "outputs" / "neutralized"
    runner = FactorResearchRunner(
        project_root=tmp_path / "database",
        db_path=tmp_path / "database" / "data" / "share_quant.duckdb",
        output_dir=tmp_path / "outputs" / "single_factor",
        neutralized_output_dir=neutralized,
    )

    assert runner.neutralized_output_dir == neutralized


def test_normalize_winsorizes_and_applies_negative_direction() -> None:
    panel = pd.DataFrame(
        [
            {"factor": "PE_TTM", "trade_date": "20220131", "ts_code": "A", "raw_value": 10.0},
            {"factor": "PE_TTM", "trade_date": "20220131", "ts_code": "B", "raw_value": 20.0},
            {"factor": "PE_TTM", "trade_date": "20220131", "ts_code": "C", "raw_value": 1000.0},
        ]
    )

    scores = normalize_factor_panel(panel, specs=[FactorSpec("PE_TTM", "估值", "市盈率", "pe", -1)])
    ordered = scores.sort_values("score", ascending=False)["ts_code"].tolist()

    assert ordered[0] == "A"
    assert scores["score"].notna().all()


def test_financial_factor_query_uses_visible_asof_interval_without_future_data() -> None:
    con = duckdb.connect(":memory:")
    con.register("signal_dates", pd.DataFrame({"trade_date": ["20220131"]}))
    con.execute(
        """
        create view v_fina_indicator_asof_intervals as
        select * from (
            values
                ('000001.SZ', '20220105', '20220331', '20220105', '20220201', 1.0),
                ('000001.SZ', '20211220', '20211231', '20211220', '20220105', 5.0),
                ('000001.SZ', '20220201', '20220630', '20220201', null, 9.0)
        ) as t(ts_code, ann_date, end_date, visible_from, next_visible_from, roe)
        """
    )
    con.execute(
        """
        create view v_stock_universe_daily as
        select
            '000001.SZ' as ts_code,
            '20220131' as trade_date,
            true as is_listed_on_date,
            false as is_suspended,
            false as is_st_name
        """
    )
    runner = FactorResearchRunner(Path("."), Path("dummy.duckdb"), Path("outputs"))

    result = con.execute(runner._financial_factor_query("ROE", "roe"), ["20210101", "20221231"]).fetchdf()

    assert result.to_dict("records") == [
        {"factor": "ROE", "trade_date": "20220131", "ts_code": "000001.SZ", "raw_value": 1.0}
    ]


def test_financial_factor_query_takes_latest_visible_report_not_average() -> None:
    con = duckdb.connect(":memory:")
    con.register("signal_dates", pd.DataFrame({"trade_date": ["20220430"]}))
    con.execute(
        """
        create view v_fina_indicator_asof_intervals as
        select * from (
            values
                ('000001.SZ', '20220105', '20211231', '20220105', null, 1.0),
                ('000001.SZ', '20220420', '20220331', '20220420', null, 3.0)
        ) as t(ts_code, ann_date, end_date, visible_from, next_visible_from, roe)
        """
    )
    con.execute(
        """
        create view v_stock_universe_daily as
        select
            '000001.SZ' as ts_code,
            '20220430' as trade_date,
            true as is_listed_on_date,
            false as is_suspended,
            false as is_st_name
        """
    )
    runner = FactorResearchRunner(Path("."), Path("dummy.duckdb"), Path("outputs"))

    result = con.execute(runner._financial_factor_query("ROE", "roe"), ["20210101", "20221231"]).fetchdf()

    assert result.to_dict("records") == [
        {"factor": "ROE", "trade_date": "20220430", "ts_code": "000001.SZ", "raw_value": 3.0}
    ]


def test_financial_factor_query_prefers_latest_period_over_later_old_revision() -> None:
    con = duckdb.connect(":memory:")
    con.register("signal_dates", pd.DataFrame({"trade_date": ["20220430"]}))
    con.execute(
        """
        create view v_fina_indicator_asof_intervals as
        select * from (
            values
                ('000001.SZ', '20220420', '20220331', '20220420', null, 3.0),
                ('000001.SZ', '20220429', '20211231', '20220429', null, 9.0)
        ) as t(ts_code, ann_date, end_date, visible_from, next_visible_from, roe)
        """
    )
    con.execute(
        """
        create view v_stock_universe_daily as
        select
            '000001.SZ' as ts_code,
            '20220430' as trade_date,
            true as is_listed_on_date,
            false as is_suspended,
            false as is_st_name
        """
    )
    runner = FactorResearchRunner(Path("."), Path("dummy.duckdb"), Path("outputs"))

    result = con.execute(runner._financial_factor_query("ROE", "roe"), ["20210101", "20221231"]).fetchdf()

    assert result.to_dict("records") == [
        {"factor": "ROE", "trade_date": "20220430", "ts_code": "000001.SZ", "raw_value": 3.0}
    ]


def test_financial_factor_query_excludes_expired_visible_interval() -> None:
    con = duckdb.connect(":memory:")
    con.register("signal_dates", pd.DataFrame({"trade_date": ["20220215"]}))
    con.execute(
        """
        create view v_fina_indicator_asof_intervals as
        select * from (
            values
                ('000001.SZ', '20220105', '20211231', '20220105', null, 1.0),
                ('000001.SZ', '20220120', '20210930', '20220120', '20220201', 9.0)
        ) as t(ts_code, ann_date, end_date, visible_from, next_visible_from, roe)
        """
    )
    con.execute(
        """
        create view v_stock_universe_daily as
        select
            '000001.SZ' as ts_code,
            '20220215' as trade_date,
            true as is_listed_on_date,
            false as is_suspended,
            false as is_st_name
        """
    )
    runner = FactorResearchRunner(Path("."), Path("dummy.duckdb"), Path("outputs"))

    result = con.execute(runner._financial_factor_query("ROE", "roe"), ["20210101", "20221231"]).fetchdf()

    assert result.to_dict("records") == [
        {"factor": "ROE", "trade_date": "20220215", "ts_code": "000001.SZ", "raw_value": 1.0}
    ]


def test_rebalance_calendar_uses_month_end_signal_and_next_trade_execution() -> None:
    dates = pd.Series(["20220128", "20220131", "20220201", "20220228", "20220301"])

    calendar = build_rebalance_calendar(dates, "20220101", "20220228")

    assert calendar.to_dict("records") == [
        {"signal_date": "20220131", "exec_date": "20220201"}
    ]


def test_select_top_quantile_weights_normalizes_to_one() -> None:
    scores = pd.DataFrame(
        [
            {"factor": "F", "trade_date": "20220131", "ts_code": f"S{i}", "score": float(i)}
            for i in range(10)
        ]
    )

    weights = select_top_quantile_weights(scores, quantile=0.2)

    assert set(weights["ts_code"]) == {"S8", "S9"}
    assert weights["weight"].sum() == 1.0


def test_execution_universe_filter_runs_before_top_quantile_selection() -> None:
    scores = pd.DataFrame(
        [
            {"factor": "F", "trade_date": "20220131", "ts_code": "A", "score": 5.0},
            {"factor": "F", "trade_date": "20220131", "ts_code": "B", "score": 4.0},
            {"factor": "F", "trade_date": "20220131", "ts_code": "C", "score": 3.0},
            {"factor": "F", "trade_date": "20220131", "ts_code": "D", "score": 2.0},
            {"factor": "F", "trade_date": "20220131", "ts_code": "E", "score": 1.0},
        ]
    )
    calendar = pd.DataFrame([{"signal_date": "20220131", "exec_date": "20220201"}])
    execution_universe = pd.DataFrame(
        [
            {"exec_date": "20220201", "ts_code": "B"},
            {"exec_date": "20220201", "ts_code": "C"},
            {"exec_date": "20220201", "ts_code": "D"},
            {"exec_date": "20220201", "ts_code": "E"},
        ]
    )

    tradable_scores = filter_scores_for_execution_universe(scores, calendar, execution_universe)
    weights = select_top_quantile_weights(tradable_scores, quantile=0.4)

    assert set(tradable_scores["ts_code"]) == {"B", "C", "D", "E"}
    assert weights.to_dict("records") == [
        {"factor": "F", "trade_date": "20220131", "ts_code": "B", "weight": 0.5},
        {"factor": "F", "trade_date": "20220131", "ts_code": "C", "weight": 0.5},
    ]


def test_suspended_missing_returns_are_zero_and_transaction_cost_is_deducted() -> None:
    weights = pd.DataFrame(
        [
            {"factor": "F", "trade_date": "20220131", "ts_code": "A", "weight": 0.5},
            {"factor": "F", "trade_date": "20220131", "ts_code": "B", "weight": 0.5},
        ]
    )
    returns = pd.DataFrame(
        [
            {"trade_date": "20220202", "ts_code": "A", "return_adjusted": 0.10},
            {"trade_date": "20220203", "ts_code": "A", "return_adjusted": 0.00},
            {"trade_date": "20220203", "ts_code": "B", "return_adjusted": 0.20},
        ]
    )
    calendar = pd.DataFrame([{"signal_date": "20220131", "exec_date": "20220201"}])
    suspensions = pd.DataFrame([{"trade_date": "20220202", "ts_code": "B"}])

    daily, turnover = backtest_monthly_top_quantile(
        weights,
        returns,
        calendar,
        "20220203",
        transaction_cost=0.001,
        suspensions=suspensions,
    )

    assert turnover.loc[0, "turnover"] == 1.0
    assert daily.loc[daily["trade_date"] == "20220202", "portfolio_return"].iloc[0] == 0.049
    assert daily.loc[daily["trade_date"] == "20220202", "missing_return_count"].iloc[0] == 1
    assert daily.loc[daily["trade_date"] == "20220202", "suspended_return_count"].iloc[0] == 1
    assert daily.loc[daily["trade_date"] == "20220202", "invalid_missing_return_count"].iloc[0] == 0
    assert daily.loc[daily["trade_date"] == "20220203", "portfolio_return"].iloc[0] == 0.10


def test_invalid_missing_returns_are_excluded_and_reweighted() -> None:
    weights = pd.DataFrame(
        [
            {"factor": "F", "trade_date": "20220131", "ts_code": "A", "weight": 0.5},
            {"factor": "F", "trade_date": "20220131", "ts_code": "B", "weight": 0.5},
        ]
    )
    returns = pd.DataFrame(
        [
            {"trade_date": "20220202", "ts_code": "A", "return_adjusted": 0.10},
            {"trade_date": "20220203", "ts_code": "A", "return_adjusted": 0.00},
            {"trade_date": "20220203", "ts_code": "B", "return_adjusted": 0.20},
        ]
    )
    calendar = pd.DataFrame([{"signal_date": "20220131", "exec_date": "20220201"}])

    daily, _turnover = backtest_monthly_top_quantile(weights, returns, calendar, "20220203", transaction_cost=0.001)

    first = daily.loc[daily["trade_date"] == "20220202"].iloc[0]
    assert first["portfolio_return"] == 0.099
    assert first["missing_return_count"] == 1
    assert first["suspended_return_count"] == 0
    assert first["invalid_missing_return_count"] == 1
    assert first["invalid_missing_weight"] == 0.5


def test_one_way_turnover_compares_rebalance_weights() -> None:
    previous = pd.Series({"A": 0.5, "B": 0.5})
    current = pd.Series({"B": 0.5, "C": 0.5})

    assert one_way_turnover(previous, current) == 0.5


def test_forward_period_returns_start_after_exec_and_include_next_exec() -> None:
    returns = pd.DataFrame(
        [
            {"trade_date": "20220201", "ts_code": "A", "return_adjusted": 0.10},
            {"trade_date": "20220202", "ts_code": "A", "return_adjusted": 0.20},
            {"trade_date": "20220301", "ts_code": "A", "return_adjusted": 0.10},
        ]
    )
    calendar = pd.DataFrame(
        [
            {"signal_date": "20220131", "exec_date": "20220201"},
            {"signal_date": "20220228", "exec_date": "20220301"},
        ]
    )

    forward = build_forward_period_returns(returns, calendar, "20220301", ts_codes=["A"])

    assert forward.to_dict("records") == [
        {
            "signal_date": "20220131",
            "exec_date": "20220201",
            "ts_code": "A",
            "forward_return": 0.32000000000000006,
            "holding_days": 2,
            "missing_return_count": 0,
            "suspended_return_count": 0,
            "invalid_missing_return_count": 0,
        }
    ]


def test_forward_period_returns_exclude_invalid_missing_but_keep_suspensions() -> None:
    returns = pd.DataFrame(
        [
            {"trade_date": "20220202", "ts_code": "A", "return_adjusted": 0.10},
            {"trade_date": "20220203", "ts_code": "A", "return_adjusted": 0.20},
            {"trade_date": "20220203", "ts_code": "B", "return_adjusted": 0.20},
        ]
    )
    calendar = pd.DataFrame([{"signal_date": "20220131", "exec_date": "20220201"}])
    suspensions = pd.DataFrame([{"trade_date": "20220202", "ts_code": "B"}])

    forward = build_forward_period_returns(returns, calendar, "20220203", ts_codes=["A", "B", "C"], suspensions=suspensions)

    by_code = forward.set_index("ts_code")
    assert by_code.loc["A", "forward_return"] == 0.32000000000000006
    assert by_code.loc["B", "forward_return"] == 0.19999999999999996
    assert pd.isna(by_code.loc["C", "forward_return"])
    assert by_code.loc["B", "suspended_return_count"] == 1
    assert by_code.loc["C", "invalid_missing_return_count"] == 2


def test_ic_uses_matching_signal_period_only() -> None:
    scores = pd.DataFrame(
        [
            {"factor": "F", "trade_date": "20220131", "ts_code": "A", "score": 1.0},
            {"factor": "F", "trade_date": "20220131", "ts_code": "B", "score": 2.0},
            {"factor": "F", "trade_date": "20220131", "ts_code": "C", "score": 3.0},
            {"factor": "F", "trade_date": "20220228", "ts_code": "A", "score": 3.0},
            {"factor": "F", "trade_date": "20220228", "ts_code": "B", "score": 2.0},
            {"factor": "F", "trade_date": "20220228", "ts_code": "C", "score": 1.0},
        ]
    )
    forward = pd.DataFrame(
        [
            {"signal_date": "20220131", "ts_code": "A", "forward_return": 0.01},
            {"signal_date": "20220131", "ts_code": "B", "forward_return": 0.02},
            {"signal_date": "20220131", "ts_code": "C", "forward_return": 0.03},
        ]
    )

    ic = compute_ic_by_period(scores, forward)

    first = ic[ic["trade_date"] == "20220131"].iloc[0]
    second = ic[ic["trade_date"] == "20220228"].iloc[0]
    assert abs(first["ic"] - 1.0) < 1e-12
    assert abs(first["rank_ic"] - 1.0) < 1e-12
    assert pd.isna(second["ic"])
    assert second["sample_count"] == 0


def test_decile_assignment_and_long_short_return_direction() -> None:
    scores = pd.DataFrame(
        [
            {"factor": "F", "trade_date": "20220131", "ts_code": f"S{i}", "score": float(i)}
            for i in range(10)
        ]
    )
    forward = pd.DataFrame(
        [
            {"signal_date": "20220131", "ts_code": f"S{i}", "forward_return": float(i) / 100}
            for i in range(10)
        ]
    )

    deciles = assign_score_deciles(scores, bucket_count=10)
    decile_returns = compute_decile_returns(deciles, forward)
    long_short = compute_long_short_returns(decile_returns, bucket_count=10)

    assert deciles.loc[deciles["ts_code"] == "S9", "decile"].iloc[0] == 10
    assert deciles.loc[deciles["ts_code"] == "S0", "decile"].iloc[0] == 1
    assert long_short["long_short_return"].iloc[0] == 0.09


def test_decile_returns_ignore_missing_forward_returns() -> None:
    deciles = pd.DataFrame(
        [
            {"factor": "F", "trade_date": "20220131", "ts_code": "A", "decile": 1},
            {"factor": "F", "trade_date": "20220131", "ts_code": "B", "decile": 1},
        ]
    )
    forward = pd.DataFrame(
        [
            {"signal_date": "20220131", "ts_code": "A", "forward_return": 0.10},
            {"signal_date": "20220131", "ts_code": "B", "forward_return": pd.NA},
        ]
    )

    decile_returns = compute_decile_returns(deciles, forward)

    assert decile_returns.loc[0, "average_forward_return"] == 0.10
    assert decile_returns.loc[0, "stock_count"] == 1


def test_neutralized_scores_remove_industry_and_size_exposure() -> None:
    scores = pd.DataFrame(
        [
            {"factor": "F", "trade_date": "20220131", "ts_code": "A1", "score": 13.0},
            {"factor": "F", "trade_date": "20220131", "ts_code": "A2", "score": 13.0},
            {"factor": "F", "trade_date": "20220131", "ts_code": "A3", "score": 16.0},
            {"factor": "F", "trade_date": "20220131", "ts_code": "B1", "score": 21.0},
            {"factor": "F", "trade_date": "20220131", "ts_code": "B2", "score": 25.0},
            {"factor": "F", "trade_date": "20220131", "ts_code": "B3", "score": 26.0},
        ]
    )
    exposures = pd.DataFrame(
        [
            {"trade_date": "20220131", "ts_code": "A1", "industry": "A", "log_total_mv": 1.0},
            {"trade_date": "20220131", "ts_code": "A2", "industry": "A", "log_total_mv": 2.0},
            {"trade_date": "20220131", "ts_code": "A3", "industry": "A", "log_total_mv": 3.0},
            {"trade_date": "20220131", "ts_code": "B1", "industry": "B", "log_total_mv": 1.0},
            {"trade_date": "20220131", "ts_code": "B2", "industry": "B", "log_total_mv": 2.0},
            {"trade_date": "20220131", "ts_code": "B3", "industry": "B", "log_total_mv": 3.0},
        ]
    )

    neutralized = neutralize_scores(scores, exposures)

    assert abs(neutralized["neutralized_score"].astype(float).corr(neutralized["log_total_mv"])) < 1e-12
    assert neutralized.groupby("industry")["neutralized_score"].mean().abs().max() < 1e-12


def test_load_exposures_uses_industry_interval_without_future_record() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        parquet_path = root / "daily_basic.parquet"
        con = duckdb.connect(":memory:")
        con.register(
            "daily_basic_fixture",
            pd.DataFrame([{"trade_date": "20220131", "ts_code": "000001.SZ", "total_mv": 100.0}]),
        )
        con.execute("copy daily_basic_fixture to ? (format parquet)", [str(parquet_path)])
        con.execute(
            """
            create view v_industry_data as
            select * from (
                values
                    ('000001.SZ', '银行', '20210101', '20220201'),
                    ('000001.SZ', '电子', '20220201', null)
            ) as t(ts_code, l1_name, in_date, out_date)
            """
        )
        runner = FactorDiagnosticsRunner(root, root / "dummy.duckdb", root / "outputs")
        runner._research._silver_path = lambda _dataset: str(parquet_path).replace("\\", "/")

        exposures = runner._load_exposures(con, pd.Series(["20220131"]))

        assert exposures.loc[0, "industry"] == "银行"
        assert exposures.loc[0, "log_total_mv"] == math.log(100.0)


def test_composite_equal_ignores_missing_scores_and_enforces_min_factor_count() -> None:
    scores = pd.DataFrame(
        [
            {"factor": "F1", "trade_date": "20220131", "ts_code": "A", "neutralized_score": 1.0},
            {"factor": "F2", "trade_date": "20220131", "ts_code": "A", "neutralized_score": 3.0},
            {"factor": "F1", "trade_date": "20220131", "ts_code": "B", "neutralized_score": 2.0},
            {"factor": "F2", "trade_date": "20220131", "ts_code": "C", "neutralized_score": 4.0},
            {"factor": "F3", "trade_date": "20220131", "ts_code": "C", "neutralized_score": 8.0},
        ]
    )

    composite, _weights = build_composite_scores(scores, min_factor_count=2, rankic_min_periods=1)
    equal = composite[composite["factor"] == COMPOSITE_EQUAL].set_index("ts_code")

    assert equal.loc["A", "score"] == 2.0
    assert pd.isna(equal.loc["B", "score"])
    assert equal.loc["C", "score"] == 6.0


def test_rolling_rankic_weights_use_only_past_signal_periods() -> None:
    scores = pd.DataFrame(
        [
            {"factor": "F1", "trade_date": "20220131", "ts_code": "A", "neutralized_score": 10.0},
            {"factor": "F2", "trade_date": "20220131", "ts_code": "A", "neutralized_score": 0.0},
            {"factor": "F1", "trade_date": "20220228", "ts_code": "A", "neutralized_score": 10.0},
            {"factor": "F2", "trade_date": "20220228", "ts_code": "A", "neutralized_score": 0.0},
        ]
    )
    rank_ic = pd.DataFrame(
        [
            {"factor": "F1", "trade_date": "20220131", "rank_ic": 0.8},
            {"factor": "F2", "trade_date": "20220131", "rank_ic": 0.2},
            {"factor": "F1", "trade_date": "20220228", "rank_ic": -1.0},
            {"factor": "F2", "trade_date": "20220228", "rank_ic": 1.0},
        ]
    )

    composite, weights = build_composite_scores(
        scores,
        rank_ic_by_period=rank_ic,
        min_factor_count=2,
        rankic_min_periods=1,
    )
    current = composite[
        (composite["factor"] == COMPOSITE_ROLLING_RANKIC)
        & (composite["trade_date"] == "20220228")
        & (composite["ts_code"] == "A")
    ].iloc[0]
    current_weights = weights[
        (weights["composite_factor"] == COMPOSITE_ROLLING_RANKIC)
        & (weights["trade_date"] == "20220228")
    ].set_index("source_factor")

    assert abs(current["score"] - 8.0) < 1e-12
    assert abs(current_weights.loc["F1", "weight"] - 0.8) < 1e-12
    assert abs(current_weights.loc["F2", "weight"] - 0.2) < 1e-12


def test_rolling_rankic_falls_back_to_equal_for_negative_or_insufficient_history() -> None:
    scores = pd.DataFrame(
        [
            {"factor": "F1", "trade_date": "20220131", "ts_code": "A", "neutralized_score": 10.0},
            {"factor": "F2", "trade_date": "20220131", "ts_code": "A", "neutralized_score": 0.0},
            {"factor": "F1", "trade_date": "20220228", "ts_code": "A", "neutralized_score": 10.0},
            {"factor": "F2", "trade_date": "20220228", "ts_code": "A", "neutralized_score": 0.0},
        ]
    )
    rank_ic = pd.DataFrame(
        [
            {"factor": "F1", "trade_date": "20220131", "rank_ic": -0.4},
            {"factor": "F2", "trade_date": "20220131", "rank_ic": -0.2},
        ]
    )

    negative, negative_weights = build_composite_scores(
        scores,
        rank_ic_by_period=rank_ic,
        min_factor_count=2,
        rankic_min_periods=1,
    )
    insufficient, insufficient_weights = build_composite_scores(
        scores,
        rank_ic_by_period=rank_ic,
        min_factor_count=2,
        rankic_min_periods=2,
    )
    negative_score = negative[
        (negative["factor"] == COMPOSITE_ROLLING_RANKIC)
        & (negative["trade_date"] == "20220228")
        & (negative["ts_code"] == "A")
    ]["score"].iloc[0]
    insufficient_score = insufficient[
        (insufficient["factor"] == COMPOSITE_ROLLING_RANKIC)
        & (insufficient["trade_date"] == "20220228")
        & (insufficient["ts_code"] == "A")
    ]["score"].iloc[0]
    negative_current = negative_weights[
        (negative_weights["composite_factor"] == COMPOSITE_ROLLING_RANKIC)
        & (negative_weights["trade_date"] == "20220228")
    ]
    insufficient_current = insufficient_weights[
        (insufficient_weights["composite_factor"] == COMPOSITE_ROLLING_RANKIC)
        & (insufficient_weights["trade_date"] == "20220228")
    ]

    assert negative_score == 5.0
    assert insufficient_score == 5.0
    assert set(negative_current["weight_source"]) == {"fallback_equal"}
    assert set(insufficient_current["weight_source"]) == {"fallback_equal"}
    assert negative_current["weight"].tolist() == [0.5, 0.5]
    assert insufficient_current["weight"].tolist() == [0.5, 0.5]


def test_score_layers_put_highest_scores_in_top_bucket_and_weights_sum_to_one() -> None:
    scores = pd.DataFrame(
        [
            {"factor": COMPOSITE_EQUAL, "trade_date": "20220131", "ts_code": f"S{i}", "score": float(i)}
            for i in range(10)
        ]
    )

    layers = assign_score_layers(scores, bucket_count=10)
    weights = build_layer_weights(layers)

    assert layers.loc[layers["ts_code"] == "S9", "bucket"].iloc[0] == 10
    assert layers.loc[layers["ts_code"] == "S0", "bucket"].iloc[0] == 1
    assert weights.groupby(["factor", "trade_date"])["weight"].sum().round(12).eq(1.0).all()


def test_multifactor_results_keep_d10_minus_d1_direction() -> None:
    composite_scores = pd.DataFrame(
        [
            {"factor": COMPOSITE_EQUAL, "trade_date": "20220131", "ts_code": "A", "score": 1.0, "available_factor_count": 2, "weight_source": "equal"},
            {"factor": COMPOSITE_EQUAL, "trade_date": "20220131", "ts_code": "B", "score": 2.0, "available_factor_count": 2, "weight_source": "equal"},
            {"factor": COMPOSITE_ROLLING_RANKIC, "trade_date": "20220131", "ts_code": "A", "score": 1.0, "available_factor_count": 2, "weight_source": "fallback_equal"},
            {"factor": COMPOSITE_ROLLING_RANKIC, "trade_date": "20220131", "ts_code": "B", "score": 2.0, "available_factor_count": 2, "weight_source": "fallback_equal"},
        ]
    )
    factor_weights = pd.DataFrame(
        [
            {"trade_date": "20220131", "composite_factor": COMPOSITE_EQUAL, "source_factor": "F1", "weight": 0.5},
            {"trade_date": "20220131", "composite_factor": COMPOSITE_EQUAL, "source_factor": "F2", "weight": 0.5},
        ]
    )
    returns = pd.DataFrame(
        [
            {"trade_date": "20220202", "ts_code": "A", "return_adjusted": 0.01},
            {"trade_date": "20220202", "ts_code": "B", "return_adjusted": 0.05},
        ]
    )
    calendar = pd.DataFrame([{"signal_date": "20220131", "exec_date": "20220201"}])
    benchmark = pd.DataFrame([{"trade_date": "20220202", "benchmark_return": 0.0}])
    forward = pd.DataFrame(
        [
            {"signal_date": "20220131", "ts_code": "A", "forward_return": 0.01},
            {"signal_date": "20220131", "ts_code": "B", "forward_return": 0.05},
        ]
    )

    results = build_multifactor_results(
        composite_scores=composite_scores,
        factor_weights=factor_weights,
        returns=returns,
        rebalance_calendar=calendar,
        end_date="20220202",
        benchmark_returns=benchmark,
        forward_returns=forward,
        bucket_count=2,
        transaction_cost=0.0,
    )

    long_short = results["long_short_returns"].set_index("composite_factor")
    assert long_short.loc[COMPOSITE_EQUAL, "long_short_return"] == 0.04
    assert set(results["top_layer_weights"]["ts_code"]) == {"B"}
    assert not results["layer_metrics"].empty


def test_multifactor_cli_parser_accepts_planned_arguments() -> None:
    args = build_multifactor_parser().parse_args(
        [
            "--start",
            "2022-01-01",
            "--end",
            "2022-12-31",
            "--bucket-count",
            "5",
            "--rankic-window",
            "8",
            "--rankic-min-periods",
            "3",
            "--min-factor-count",
            "4",
            "--transaction-cost",
            "0.002",
        ]
    )

    assert args.bucket_count == 5
    assert args.rankic_window == 8
    assert args.rankic_min_periods == 3
    assert args.min_factor_count == 4
    assert args.transaction_cost == 0.002
