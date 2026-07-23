from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from pathlib import Path

import duckdb
import pandas as pd

from factor_research.multifactor import COMPOSITE_ROLLING_RANKIC
from factor_research.walk_forward_champion import (
    WalkForwardConfig,
    _run_id,
    _write_export,
    build_long_short_period_returns,
    build_parser,
    build_point_in_time_composites,
    build_walk_forward_schedule,
    load_experiment_config,
    select_champion,
)


def _monthly_calendar(count: int, start: str = "2020-01-31") -> pd.DataFrame:
    signals = pd.date_range(start, periods=count, freq="ME")
    return pd.DataFrame(
        {
            "signal_date": signals.strftime("%Y%m%d"),
            "exec_date": (signals + pd.Timedelta(days=1)).strftime("%Y%m%d"),
        }
    )


def test_stage_one_config_freezes_expected_candidates_and_directions() -> None:
    root = Path(__file__).resolve().parents[1]
    artifact, config = load_experiment_config(root / "configs" / "walk_forward_champion.yaml")

    assert artifact.schema_version == 1
    assert len(artifact.factor_ids) == 11
    assert artifact.factor_directions["PE_TTM"] == -1
    assert {candidate["id"] for candidate in artifact.candidates} == {
        "Composite_Equal",
        "Composite_RollingRankIC",
    }
    assert config == WalkForwardConfig()
    assert len(artifact.sha256) == 64


def test_walk_forward_schedule_enforces_completed_history_embargo_and_non_overlap() -> None:
    calendar = _monthly_calendar(53)
    config = WalkForwardConfig()
    end_date = "20240630"

    schedule = build_walk_forward_schedule(calendar, config, end_date)

    complete = schedule[schedule["status"] == "complete"].reset_index(drop=True)
    assert not complete.empty
    first = complete.iloc[0]
    periods = calendar.copy()
    periods["next_exec_date"] = periods["exec_date"].shift(-1)
    validation_dates = first["validation_signal_dates"]
    last_validation = periods[periods["signal_date"] == validation_dates[-1]].iloc[0]
    embargo = periods[periods["signal_date"] == first["embargo_signal_date"]].iloc[0]

    assert len(first["train_signal_dates"]) == 24
    assert len(validation_dates) == 12
    assert len(first["oos_signal_dates"]) == 6
    assert last_validation["next_exec_date"] <= first["decision_signal_date"]
    assert embargo["next_exec_date"] > first["decision_signal_date"]
    for left, right in zip(complete["oos_signal_dates"], complete["oos_signal_dates"].iloc[1:]):
        assert set(left).isdisjoint(right)


def test_walk_forward_schedule_records_incomplete_last_fold() -> None:
    calendar = _monthly_calendar(45)
    periods = calendar.copy()
    periods["next_exec_date"] = periods["exec_date"].shift(-1)
    schedule = build_walk_forward_schedule(calendar, WalkForwardConfig(), str(periods.iloc[-2]["next_exec_date"]))

    assert schedule.iloc[-1]["status"] == "incomplete"
    assert schedule.iloc[-1]["oos_completed_periods"] < 6


def _selection_inputs(a_sharpe_returns: list[float], b_sharpe_returns: list[float]) -> tuple[pd.DataFrame, pd.DataFrame, list[str], list[str]]:
    dates = [f"D{i:02d}" for i in range(36)]
    train = dates[:24]
    validation = dates[24:]
    rank_ic = pd.DataFrame(
        [
            {"factor": factor, "trade_date": date, "rank_ic": 0.05 if factor == "A" else 0.04}
            for factor in ["A", "B"]
            for date in dates
        ]
    )
    long_short = pd.DataFrame(
        [
            {
                "factor": factor,
                "trade_date": date,
                "net_long_short_return": value,
                "two_leg_turnover": 0.4 if factor == "A" else 0.3,
            }
            for factor, values in [("A", a_sharpe_returns), ("B", b_sharpe_returns)]
            for date, value in zip(validation, values)
        ]
    )
    return rank_ic, long_short, train, validation


def test_champion_switch_margin_retains_incumbent_then_allows_clear_challenger() -> None:
    a = [0.01, 0.02] * 6
    b_close = [0.0101, 0.0201] * 6
    rank_ic, returns, train, validation = _selection_inputs(a, b_close)
    config = replace(WalkForwardConfig(), min_train_rankic_periods=1, min_validation_return_periods=2)

    log, champion, reason = select_champion(["A", "B"], rank_ic, returns, train, validation, "A", config)

    assert champion == "A"
    assert reason == "incumbent_retained_by_margin"
    assert log.loc[log["selected"], "factor_id"].tolist() == ["A"]

    b_clear = [0.020, 0.021] * 6
    rank_ic, returns, train, validation = _selection_inputs(a, b_clear)
    _log, champion, reason = select_champion(["A", "B"], rank_ic, returns, train, validation, "A", config)
    assert champion == "B"
    assert reason == "challenger_margin_met"


def test_champion_tie_breaks_on_turnover_then_rankic_then_id() -> None:
    values = [0.01, 0.02] * 6
    rank_ic, returns, train, validation = _selection_inputs(values, values)
    config = replace(WalkForwardConfig(), min_train_rankic_periods=1, min_validation_return_periods=2)

    log, champion, reason = select_champion(["A", "B"], rank_ic, returns, train, validation, None, config)

    assert champion == "B"
    assert reason == "initial_champion"
    assert log.iloc[0]["factor_id"] == "B"


def test_no_eligible_champion_does_not_fall_back() -> None:
    rank_ic = pd.DataFrame(columns=["factor", "trade_date", "rank_ic"])
    returns = pd.DataFrame(columns=["factor", "trade_date", "net_long_short_return", "two_leg_turnover"])

    log, champion, reason = select_champion(["A"], rank_ic, returns, [], [], None, WalkForwardConfig())

    assert champion is None
    assert reason == "no_eligible_factor"
    assert not log["selected"].any()


def test_long_short_period_return_deducts_both_leg_turnover_costs() -> None:
    scores = pd.DataFrame(
        [
            {"factor": "F", "trade_date": "20220131", "ts_code": f"S{i}", "neutralized_score": float(i)}
            for i in range(10)
        ]
    )
    forward = pd.DataFrame(
        [
            {"signal_date": "20220131", "ts_code": f"S{i}", "forward_return": i / 100.0}
            for i in range(10)
        ]
    )

    result = build_long_short_period_returns(scores, forward, transaction_cost=0.001)

    assert result.loc[0, "gross_long_short_return"] == 0.09
    assert result.loc[0, "two_leg_turnover"] == 2.0
    assert result.loc[0, "transaction_cost"] == 0.002
    assert result.loc[0, "net_long_short_return"] == 0.088


def test_point_in_time_rankic_weights_ignore_unfinished_period() -> None:
    calendar = pd.DataFrame(
        [
            {"signal_date": "20220131", "exec_date": "20220201"},
            {"signal_date": "20220228", "exec_date": "20220301"},
            {"signal_date": "20220331", "exec_date": "20220401"},
            {"signal_date": "20220429", "exec_date": "20220502"},
        ]
    )
    scores = pd.DataFrame(
        [
            {"factor": factor, "trade_date": date, "ts_code": "A", "neutralized_score": value}
            for date in ["20220131", "20220228", "20220331"]
            for factor, value in [("F1", 10.0), ("F2", 0.0)]
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

    composite, weights = build_point_in_time_composites(
        scores,
        rank_ic,
        calendar,
        ["F1", "F2"],
        rankic_window=12,
        rankic_min_periods=1,
        min_factor_count=2,
    )
    current = composite[
        (composite["factor"] == COMPOSITE_ROLLING_RANKIC)
        & (composite["trade_date"] == "20220331")
    ].iloc[0]
    current_weights = weights[
        (weights["composite_factor"] == COMPOSITE_ROLLING_RANKIC)
        & (weights["trade_date"] == "20220331")
    ].set_index("source_factor")

    assert current["score"] == 8.0
    assert current_weights.loc["F1", "weight"] == 0.8
    assert current_weights.loc["F2", "weight"] == 0.2
    assert set(current_weights["weight_as_of_date"]) == {"20220301"}


def test_run_id_is_deterministic_and_parser_accepts_overrides() -> None:
    config = WalkForwardConfig()
    assert _run_id("abc", config, "20260707") == _run_id("abc", config, "20260707")
    args = build_parser().parse_args(
        ["--train-periods", "30", "--validation-periods", "9", "--oos-periods", "3", "--step-periods", "3"]
    )
    assert (args.train_periods, args.validation_periods, args.oos_periods, args.step_periods) == (30, 9, 3, 3)


def test_export_contains_only_new_output_tree(tmp_path: Path) -> None:
    output = tmp_path / "outputs" / "walk_forward_champion"
    (output / "tables").mkdir(parents=True)
    (output / "images").mkdir()
    (output / "report.md").write_text("report", encoding="utf-8")
    (output / "tables" / "oos_metrics.csv").write_text("a,b\n", encoding="utf-8")
    archive = output / "exports" / "walk_forward_champion.zip"
    archive.parent.mkdir()

    _write_export(archive, output)

    with zipfile.ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == {"report.md", "tables/oos_metrics.csv"}


def test_temporary_duckdb_can_be_reopened_read_only_without_mutation(tmp_path: Path) -> None:
    database = tmp_path / "fixture.duckdb"
    with duckdb.connect(str(database)) as con:
        con.execute("create table fixture(value integer)")
        con.execute("insert into fixture values (1)")
    original = database.read_bytes()

    with duckdb.connect(str(database), read_only=True) as con:
        assert con.execute("select * from fixture").fetchall() == [(1,)]

    assert database.read_bytes() == original
    metadata = {"database_open_mode": "read_only"}
    assert json.loads(json.dumps(metadata))["database_open_mode"] == "read_only"
