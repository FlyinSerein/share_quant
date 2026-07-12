from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from .datasets import DATASETS, DatasetSpec
from .utils import iso_now


RESEARCH_VIEWS = (
    "v_stock_master",
    "v_daily_market",
    "v_financial_reports",
    "v_corporate_actions",
    "v_index_data",
    "v_industry_data",
    "v_adjusted_daily",
    "v_adjusted_returns",
    "v_stock_universe_daily",
    "v_fina_indicator_asof_intervals",
    "v_income_asof_intervals",
    "v_balancesheet_asof_intervals",
    "v_cashflow_asof_intervals",
)

OPTIONAL_PRIMARY_KEY_COLUMNS = {
    "namechange": {"end_date", "ann_date"},
    "income_vip": {"end_type"},
    "balancesheet_vip": {"end_type"},
    "cashflow_vip": {"end_type"},
    "index_member_all": {"out_date"},
}


class StorageEngine:
    def __init__(self, data_root: Path, duckdb_path: Path):
        self.data_root = Path(data_root)
        self.duckdb_path = Path(duckdb_path)

    def init(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        for layer in ("bronze", "silver", "catalog"):
            (self.data_root / layer).mkdir(parents=True, exist_ok=True)

        with self.connect() as con:
            con.execute(
                """
                create table if not exists dataset_catalog (
                    dataset varchar primary key,
                    api_name varchar not null,
                    layer varchar not null,
                    primary_key_json varchar not null,
                    strategy varchar not null,
                    date_field varchar,
                    group_name varchar,
                    default_chunk_days bigint,
                    fields_json varchar,
                    updated_at varchar not null
                )
                """
            )
            con.execute("alter table dataset_catalog add column if not exists group_name varchar")
            con.execute("alter table dataset_catalog add column if not exists default_chunk_days bigint")
            con.execute("alter table dataset_catalog add column if not exists fields_json varchar")
            con.execute(
                """
                create table if not exists sync_batches (
                    batch_id varchar primary key,
                    dataset varchar not null,
                    api_name varchar not null,
                    params_json varchar not null,
                    status varchar not null,
                    row_count bigint not null,
                    started_at varchar not null,
                    finished_at varchar,
                    error_message varchar
                )
                """
            )
            con.execute(
                """
                create table if not exists sync_status (
                    dataset varchar primary key,
                    last_success_at varchar,
                    last_batch_id varchar,
                    last_start_date varchar,
                    last_end_date varchar,
                    row_count bigint not null default 0,
                    status varchar not null
                )
                """
            )
            con.execute(
                """
                create table if not exists data_quality (
                    check_id varchar primary key,
                    dataset varchar not null,
                    check_name varchar not null,
                    status varchar not null,
                    detail varchar,
                    checked_at varchar not null
                )
                """
            )
            self.refresh_dataset_catalog(con)
            self.create_views(con)

    def connect(self) -> duckdb.DuckDBPyConnection:
        self.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
        return duckdb.connect(str(self.duckdb_path))

    def write_bronze(self, spec: DatasetSpec, frame: pd.DataFrame, batch_id: str, params: dict[str, Any]) -> Path:
        bronze = frame.copy()
        bronze["_batch_id"] = batch_id
        bronze["_api_name"] = spec.api_name
        bronze["_params_json"] = json.dumps(params, ensure_ascii=False, sort_keys=True)
        bronze["_ingested_at"] = iso_now()
        path = self.data_root / "bronze" / spec.name / f"{batch_id}.parquet"
        self._write_parquet(bronze, path)
        return path

    def upsert_silver(self, spec: DatasetSpec, frame: pd.DataFrame, batch_id: str) -> int:
        self._assert_primary_key(frame, spec)
        incoming = frame.copy()
        incoming["_batch_id"] = batch_id
        incoming["_ingested_at"] = iso_now()

        path = self.silver_path(spec.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.register("incoming", incoming)
            if path.exists():
                if self._parquet_has_columns(con, path, spec.primary_key):
                    con.execute(f"create or replace temp view existing as select * from read_parquet('{_sql_path(path)}')")
                    union_sql = "select * from existing union all by name select * from incoming"
                else:
                    union_sql = "select * from incoming"
            else:
                union_sql = "select * from incoming"

            pk_expr = ", ".join(_quote(col) for col in spec.primary_key)
            con.execute(
                f"""
                create or replace temp table deduped as
                select * exclude (_rn)
                from (
                    select
                        *,
                        row_number() over (
                            partition by {pk_expr}
                            order by _ingested_at desc, _batch_id desc
                        ) as _rn
                    from ({union_sql})
                )
                where _rn = 1
                """
            )
            con.execute("copy deduped to ? (format parquet)", [str(path)])
            row_count = con.execute("select count(*) from deduped").fetchone()[0]
        return int(row_count)

    def record_batch_start(self, batch_id: str, spec: DatasetSpec, params: dict[str, Any]) -> None:
        with self.connect() as con:
            con.execute(
                """
                insert into sync_batches
                values (?, ?, ?, ?, 'running', 0, ?, null, null)
                """,
                [batch_id, spec.name, spec.api_name, json.dumps(params, sort_keys=True), iso_now()],
            )

    def record_batch_finish(
        self,
        batch_id: str,
        spec: DatasetSpec,
        status: str,
        row_count: int,
        start_date: str | None,
        end_date: str | None,
        error_message: str | None = None,
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                update sync_batches
                set status = ?, row_count = ?, finished_at = ?, error_message = ?
                where batch_id = ?
                """,
                [status, row_count, iso_now(), error_message, batch_id],
            )
            con.execute(
                """
                insert into sync_status
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict (dataset) do update set
                    last_success_at = coalesce(excluded.last_success_at, sync_status.last_success_at),
                    last_batch_id = excluded.last_batch_id,
                    last_start_date = coalesce(excluded.last_start_date, sync_status.last_start_date),
                    last_end_date = coalesce(excluded.last_end_date, sync_status.last_end_date),
                    row_count = excluded.row_count,
                    status = excluded.status
                """,
                [spec.name, iso_now() if status == "success" else None, batch_id, start_date, end_date, row_count, status],
            )

    def refresh_dataset_catalog(self, con: duckdb.DuckDBPyConnection | None = None) -> None:
        owns_connection = con is None
        con = con or self.connect()
        try:
            now = iso_now()
            rows = [
                (
                    spec.name,
                    spec.api_name,
                    spec.layer,
                    json.dumps(spec.primary_key),
                    spec.strategy,
                    spec.date_field,
                    spec.group,
                    spec.default_chunk_days,
                    json.dumps(spec.fields),
                    now,
                )
                for spec in DATASETS.values()
            ]
            current_names = list(DATASETS)
            placeholders = ", ".join(["?"] * len(current_names))
            con.execute(f"delete from dataset_catalog where dataset not in ({placeholders})", current_names)
            con.executemany(
                """
                insert into dataset_catalog
                (
                    dataset,
                    api_name,
                    layer,
                    primary_key_json,
                    strategy,
                    date_field,
                    group_name,
                    default_chunk_days,
                    fields_json,
                    updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict (dataset) do update set
                    api_name = excluded.api_name,
                    layer = excluded.layer,
                    primary_key_json = excluded.primary_key_json,
                    strategy = excluded.strategy,
                    date_field = excluded.date_field,
                    group_name = excluded.group_name,
                    default_chunk_days = excluded.default_chunk_days,
                    fields_json = excluded.fields_json,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        finally:
            if owns_connection:
                con.close()

    def record_quality(self, dataset: str, check_name: str, status: str, detail: str) -> None:
        check_id = f"{dataset}:{check_name}:{uuid.uuid4().hex[:12]}"
        with self.connect() as con:
            con.execute(
                """
                insert into data_quality
                values (?, ?, ?, ?, ?, ?)
                """,
                [check_id, dataset, check_name, status, detail, iso_now()],
            )

    def validate_dataset(self, spec: DatasetSpec) -> tuple[str, str]:
        path = self.silver_path(spec.name)
        if not path.exists():
            return "missing", f"{spec.name} has no silver parquet file"
        with self.connect() as con:
            cols = {
                row[0]
                for row in con.execute("describe select * from read_parquet(?)", [str(path)]).fetchall()
            }
            missing = [col for col in spec.primary_key if col not in cols]
            if missing:
                return "failed", f"missing primary key columns: {', '.join(missing)}"
            row_count = con.execute("select count(*) from read_parquet(?)", [str(path)]).fetchone()[0]
            if row_count == 0:
                return "failed", "silver parquet file is empty"
            required_key_columns = [
                col for col in spec.primary_key if col not in OPTIONAL_PRIMARY_KEY_COLUMNS.get(spec.name, set())
            ]
            if required_key_columns:
                null_pk_checks = [
                    f"{_quote(col)} is null or cast({_quote(col)} as varchar) = ''"
                    for col in required_key_columns
                ]
                null_pk_count = con.execute(
                    f"select count(*) from read_parquet(?) where {' or '.join(null_pk_checks)}",
                    [str(path)],
                ).fetchone()[0]
                if null_pk_count:
                    return "failed", f"rows with null or empty primary key values in required columns: {null_pk_count}"
            pk_expr = ", ".join(_quote(col) for col in spec.primary_key)
            duplicate_count = con.execute(
                f"""
                select count(*)
                from (
                    select 1
                    from read_parquet(?)
                    group by {pk_expr}
                    having count(*) > 1
                )
                """,
                [str(path)],
            ).fetchone()[0]
            if duplicate_count:
                return "failed", f"duplicate primary key groups: {duplicate_count}"
            if spec.date_field and spec.date_field in cols:
                date_field = _quote(spec.date_field)
                bad_date_count = con.execute(
                    f"""
                    select count(*)
                    from read_parquet(?)
                    where {date_field} is null
                       or not regexp_matches(cast({date_field} as varchar), '^[0-9]{{8}}$')
                    """,
                    [str(path)],
                ).fetchone()[0]
                if bad_date_count:
                    return "failed", f"rows with invalid {spec.date_field} format: {bad_date_count}"
            return "passed", "ok"

    def create_views(self, con: duckdb.DuckDBPyConnection | None = None) -> None:
        owns_connection = con is None
        con = con or self.connect()
        try:
            view_map = {
                "v_stock_master": ["stock_basic", "namechange", "stock_company"],
                "v_daily_market": ["daily", "daily_basic", "adj_factor", "suspend_d", "stk_limit", "moneyflow"],
                "v_financial_reports": ["income_vip", "balancesheet_vip", "cashflow_vip", "fina_indicator_vip"],
                "v_corporate_actions": ["dividend", "top10_holders", "top10_floatholders"],
                "v_index_data": ["index_basic", "index_daily", "index_weight"],
                "v_industry_data": ["index_classify", "index_member_all"],
            }
            for view_name, names in view_map.items():
                select_sql = self._view_union_sql(names)
                con.execute(f"create or replace view {view_name} as {select_sql}")
            self._create_research_views(con)
            con.execute(
                """
                create or replace view v_sync_status as
                select s.*
                from sync_status s
                join dataset_catalog c using (dataset)
                """
            )
        finally:
            if owns_connection:
                con.close()

    def silver_path(self, dataset: str) -> Path:
        return self.data_root / "silver" / f"{dataset}.parquet"

    def _view_union_sql(self, names: list[str]) -> str:
        parts = []
        for name in names:
            path = self.silver_path(name)
            if path.exists():
                parts.append(f"select '{name}' as dataset, * from read_parquet('{_sql_path(path)}')")
        if not parts:
            return "select null::varchar as dataset where false"
        return " union all by name ".join(parts)

    def _create_research_views(self, con: duckdb.DuckDBPyConnection) -> None:
        if self.silver_path("daily").exists() and self.silver_path("adj_factor").exists():
            daily = _sql_path(self.silver_path("daily"))
            adj = _sql_path(self.silver_path("adj_factor"))
            daily_cols = self._silver_columns(con, "daily")
            daily_selects = [
                self._optional_column(daily_cols, "open", "double"),
                self._optional_column(daily_cols, "high", "double"),
                self._optional_column(daily_cols, "low", "double"),
                self._optional_column(daily_cols, "close", "double"),
                self._optional_column(daily_cols, "pre_close", "double"),
                self._optional_column(daily_cols, "change", "double"),
                self._optional_column(daily_cols, "pct_chg", "double"),
                self._optional_column(daily_cols, "vol", "double"),
                self._optional_column(daily_cols, "amount", "double"),
            ]
            con.execute(
                f"""
                create or replace view v_adjusted_daily as
                with daily_normalized as (
                    select
                        ts_code,
                        trade_date,
                        {", ".join(daily_selects)}
                    from read_parquet('{daily}')
                ),
                latest_factor as (
                    select ts_code, adj_factor as latest_adj_factor
                    from (
                        select
                            ts_code,
                            adj_factor,
                            row_number() over (partition by ts_code order by trade_date desc) as rn
                        from read_parquet('{adj}')
                    )
                    where rn = 1
                )
                select
                    d.ts_code,
                    d.trade_date,
                    d.open,
                    d.high,
                    d.low,
                    d.close,
                    d.pre_close,
                    d.change,
                    d.pct_chg,
                    d.vol,
                    d.amount,
                    a.adj_factor,
                    lf.latest_adj_factor,
                    d.open * a.adj_factor as open_hfq,
                    d.high * a.adj_factor as high_hfq,
                    d.low * a.adj_factor as low_hfq,
                    d.close * a.adj_factor as close_hfq,
                    case when lf.latest_adj_factor is null or lf.latest_adj_factor = 0 then null else d.open * a.adj_factor / lf.latest_adj_factor end as open_qfq,
                    case when lf.latest_adj_factor is null or lf.latest_adj_factor = 0 then null else d.high * a.adj_factor / lf.latest_adj_factor end as high_qfq,
                    case when lf.latest_adj_factor is null or lf.latest_adj_factor = 0 then null else d.low * a.adj_factor / lf.latest_adj_factor end as low_qfq,
                    case when lf.latest_adj_factor is null or lf.latest_adj_factor = 0 then null else d.close * a.adj_factor / lf.latest_adj_factor end as close_qfq
                from daily_normalized d
                left join read_parquet('{adj}') a using (ts_code, trade_date)
                left join latest_factor lf using (ts_code)
                """
            )
            con.execute(
                """
                create or replace view v_adjusted_returns as
                select
                    *,
                    close_hfq / nullif(lag(close_hfq) over (partition by ts_code order by trade_date), 0) - 1 as return_adjusted
                from v_adjusted_daily
                """
            )
        else:
            con.execute("create or replace view v_adjusted_daily as select null::varchar as ts_code, null::varchar as trade_date where false")
            con.execute("create or replace view v_adjusted_returns as select * from v_adjusted_daily")

        if self.silver_path("daily").exists() and self.silver_path("stock_basic").exists():
            daily = _sql_path(self.silver_path("daily"))
            stock = _sql_path(self.silver_path("stock_basic"))
            suspend = _sql_path(self.silver_path("suspend_d"))
            has_suspend = self.silver_path("suspend_d").exists()
            stock_cols = self._silver_columns(con, "stock_basic")
            stock_selects = [
                self._optional_column(stock_cols, "name", "varchar"),
                self._optional_column(stock_cols, "market", "varchar"),
                self._optional_column(stock_cols, "exchange", "varchar"),
                self._optional_column(stock_cols, "industry", "varchar"),
                self._optional_column(stock_cols, "list_status", "varchar"),
                self._optional_column(stock_cols, "list_date", "varchar"),
                self._optional_column(stock_cols, "delist_date", "varchar"),
                self._optional_column(stock_cols, "is_hs", "varchar"),
            ]
            suspend_join = (
                f"left join read_parquet('{suspend}') su using (ts_code, trade_date)"
                if has_suspend
                else "left join (select null::varchar as ts_code, null::varchar as trade_date where false) su using (ts_code, trade_date)"
            )
            con.execute(
                f"""
                create or replace view v_stock_universe_daily as
                with stock_normalized as (
                    select
                        ts_code,
                        {", ".join(stock_selects)}
                    from read_parquet('{stock}')
                )
                select
                    d.ts_code,
                    d.trade_date,
                    s.name,
                    s.market,
                    s.exchange,
                    s.industry,
                    s.list_status,
                    s.list_date,
                    s.delist_date,
                    s.is_hs,
                    su.ts_code is not null as is_suspended,
                    case
                        when s.list_date is not null and d.trade_date < s.list_date then false
                        when s.delist_date is not null and d.trade_date > s.delist_date then false
                        else true
                    end as is_listed_on_date,
                    case when s.name like '%ST%' then true else false end as is_st_name
                from read_parquet('{daily}') d
                left join stock_normalized s using (ts_code)
                {suspend_join}
                """
            )
        else:
            con.execute("create or replace view v_stock_universe_daily as select null::varchar as ts_code, null::varchar as trade_date where false")

        self._create_asof_interval_view(con, "fina_indicator_vip", "v_fina_indicator_asof_intervals")
        self._create_asof_interval_view(con, "income_vip", "v_income_asof_intervals")
        self._create_asof_interval_view(con, "balancesheet_vip", "v_balancesheet_asof_intervals")
        self._create_asof_interval_view(con, "cashflow_vip", "v_cashflow_asof_intervals")

    def _create_asof_interval_view(
        self,
        con: duckdb.DuckDBPyConnection,
        dataset: str,
        view_name: str,
    ) -> None:
        path = self.silver_path(dataset)
        if not path.exists():
            con.execute(f"create or replace view {view_name} as select null::varchar as ts_code, null::varchar as ann_date where false")
            return
        sql_path = _sql_path(path)
        con.execute(
            f"""
            create or replace view {view_name} as
            select
                *,
                ann_date as visible_from,
                lead(ann_date) over (
                    partition by ts_code, end_date
                    order by ann_date, _ingested_at
                ) as next_visible_from
            from read_parquet('{sql_path}')
            """
        )

    def _silver_columns(self, con: duckdb.DuckDBPyConnection, dataset: str) -> set[str]:
        path = self.silver_path(dataset)
        if not path.exists():
            return set()
        return {
            row[0]
            for row in con.execute(f"describe select * from read_parquet('{_sql_path(path)}')").fetchall()
        }

    def _optional_column(self, columns: set[str], column: str, data_type: str) -> str:
        if column in columns:
            return column
        return f"null::{data_type} as {column}"

    def _write_parquet(self, frame: pd.DataFrame, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.register("to_write", frame)
            con.execute("copy to_write to ? (format parquet)", [str(path)])

    def _assert_primary_key(self, frame: pd.DataFrame, spec: DatasetSpec) -> None:
        missing = [col for col in spec.primary_key if col not in frame.columns]
        if missing:
            raise ValueError(f"{spec.name} missing primary key columns: {', '.join(missing)}")

    def _parquet_has_columns(
        self,
        con: duckdb.DuckDBPyConnection,
        path: Path,
        columns: tuple[str, ...],
    ) -> bool:
        existing = {
            row[0]
            for row in con.execute(f"describe select * from read_parquet('{_sql_path(path)}')").fetchall()
        }
        return all(col in existing for col in columns)


def validate_all(storage: StorageEngine) -> list[tuple[str, str, str]]:
    results = []
    for spec in DATASETS.values():
        status, detail = storage.validate_dataset(spec)
        storage.record_quality(spec.name, "dataset_integrity", status, detail)
        results.append((spec.name, status, detail))
    for check_name, status, detail in validate_usability(storage):
        storage.record_quality("__database__", check_name, status, detail)
        results.append((check_name, status, detail))
    return results


def validate_usability(storage: StorageEngine) -> list[tuple[str, str, str]]:
    results = []
    storage.create_views()
    results.extend(_validate_queryable_views(storage))
    results.append(_validate_daily_has_adj_factor(storage))
    results.append(_validate_daily_covers_open_trade_days(storage))
    results.append(_validate_adjusted_daily_row_count(storage))
    return results


def _validate_queryable_views(storage: StorageEngine) -> list[tuple[str, str, str]]:
    results = []
    with storage.connect() as con:
        for view_name in RESEARCH_VIEWS:
            try:
                row_count = con.execute(f"select count(*) from {_quote(view_name)}").fetchone()[0]
            except Exception as exc:
                results.append((f"view:{view_name}", "failed", f"view is not queryable: {exc}"))
            else:
                results.append((f"view:{view_name}", "passed", f"queryable rows={row_count}"))
    return results


def _validate_daily_has_adj_factor(storage: StorageEngine) -> tuple[str, str, str]:
    if not storage.silver_path("daily").exists() or not storage.silver_path("adj_factor").exists():
        return ("cross:daily_adj_factor", "missing", "daily or adj_factor silver parquet is missing")
    with storage.connect() as con:
        daily = _sql_path(storage.silver_path("daily"))
        adj = _sql_path(storage.silver_path("adj_factor"))
        missing_count = con.execute(
            f"""
            select count(*)
            from read_parquet('{daily}') d
            anti join read_parquet('{adj}') a using (ts_code, trade_date)
            """
        ).fetchone()[0]
    if missing_count:
        return ("cross:daily_adj_factor", "failed", f"daily rows without adj_factor: {missing_count}")
    return ("cross:daily_adj_factor", "passed", "all daily rows have adj_factor")


def _validate_daily_covers_open_trade_days(storage: StorageEngine) -> tuple[str, str, str]:
    if not storage.silver_path("daily").exists() or not storage.silver_path("trade_cal").exists():
        return ("cross:daily_trade_calendar", "missing", "daily or trade_cal silver parquet is missing")
    with storage.connect() as con:
        daily = _sql_path(storage.silver_path("daily"))
        trade_cal = _sql_path(storage.silver_path("trade_cal"))
        missing_days = con.execute(
            f"""
            with daily_bounds as (
                select min(trade_date) as min_date, max(trade_date) as max_date
                from read_parquet('{daily}')
            ),
            open_days as (
                select cal_date
                from read_parquet('{trade_cal}'), daily_bounds
                where is_open = 1
                  and cal_date between min_date and max_date
            ),
            daily_days as (
                select distinct trade_date
                from read_parquet('{daily}')
            )
            select count(*)
            from open_days o
            anti join daily_days d on o.cal_date = d.trade_date
            """
        ).fetchone()[0]
    if missing_days:
        return ("cross:daily_trade_calendar", "failed", f"open trade days without daily rows: {missing_days}")
    return ("cross:daily_trade_calendar", "passed", "daily covers open trade days in its date range")


def _validate_adjusted_daily_row_count(storage: StorageEngine) -> tuple[str, str, str]:
    if not storage.silver_path("daily").exists() or not storage.silver_path("adj_factor").exists():
        return ("view:v_adjusted_daily_row_count", "missing", "daily or adj_factor silver parquet is missing")
    storage.create_views()
    with storage.connect() as con:
        daily = _sql_path(storage.silver_path("daily"))
        daily_count = con.execute(f"select count(*) from read_parquet('{daily}')").fetchone()[0]
        view_count = con.execute("select count(*) from v_adjusted_daily").fetchone()[0]
    if daily_count != view_count:
        return (
            "view:v_adjusted_daily_row_count",
            "failed",
            f"v_adjusted_daily rows={view_count}, daily rows={daily_count}",
        )
    return ("view:v_adjusted_daily_row_count", "passed", f"rows={view_count}")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _sql_path(path: Path) -> str:
    return str(path).replace("'", "''").replace("\\", "/")
