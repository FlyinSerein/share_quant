from __future__ import annotations

from dataclasses import dataclass, field

from .errors import DatasetNotFoundError


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    api_name: str
    layer: str
    primary_key: tuple[str, ...]
    strategy: str = "range"
    date_field: str | None = None
    default_params: dict[str, str] = field(default_factory=dict)
    param_sets: tuple[dict[str, str], ...] = ()
    group: str = "market"
    default_chunk_days: int | None = None
    fields: tuple[str, ...] = ()


DATASETS: dict[str, DatasetSpec] = {
    "stock_basic": DatasetSpec(
        name="stock_basic",
        api_name="stock_basic",
        layer="silver",
        primary_key=("ts_code",),
        strategy="param_sets",
        param_sets=({"list_status": "L"}, {"list_status": "D"}, {"list_status": "P"}),
        group="static",
        fields=(
            "ts_code",
            "symbol",
            "name",
            "area",
            "industry",
            "market",
            "exchange",
            "list_status",
            "list_date",
            "delist_date",
            "is_hs",
            "act_name",
            "act_ent_type",
        ),
    ),
    "trade_cal": DatasetSpec(
        name="trade_cal",
        api_name="trade_cal",
        layer="silver",
        primary_key=("exchange", "cal_date"),
        strategy="range",
        date_field="cal_date",
        default_params={"exchange": "SSE"},
        group="market",
        default_chunk_days=366,
    ),
    "namechange": DatasetSpec(
        name="namechange",
        api_name="namechange",
        layer="silver",
        primary_key=("ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"),
        strategy="paged",
        group="static",
    ),
    "stock_company": DatasetSpec(
        name="stock_company",
        api_name="stock_company",
        layer="silver",
        primary_key=("ts_code",),
        strategy="static",
        group="static",
    ),
    "daily": DatasetSpec(
        name="daily",
        api_name="daily",
        layer="silver",
        primary_key=("ts_code", "trade_date"),
        strategy="range",
        date_field="trade_date",
        group="market",
        default_chunk_days=1,
    ),
    "daily_basic": DatasetSpec(
        name="daily_basic",
        api_name="daily_basic",
        layer="silver",
        primary_key=("ts_code", "trade_date"),
        strategy="range",
        date_field="trade_date",
        group="market",
        default_chunk_days=1,
    ),
    "adj_factor": DatasetSpec(
        name="adj_factor",
        api_name="adj_factor",
        layer="silver",
        primary_key=("ts_code", "trade_date"),
        strategy="range",
        date_field="trade_date",
        group="market",
        default_chunk_days=1,
    ),
    "suspend_d": DatasetSpec(
        name="suspend_d",
        api_name="suspend_d",
        layer="silver",
        primary_key=("ts_code", "trade_date"),
        strategy="range",
        date_field="trade_date",
        group="market",
        default_chunk_days=7,
    ),
    "stk_limit": DatasetSpec(
        name="stk_limit",
        api_name="stk_limit",
        layer="silver",
        primary_key=("ts_code", "trade_date"),
        strategy="range",
        date_field="trade_date",
        group="market",
        default_chunk_days=1,
    ),
    "income_vip": DatasetSpec(
        name="income_vip",
        api_name="income_vip",
        layer="silver",
        primary_key=("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type", "update_flag"),
        strategy="paged_range",
        date_field="ann_date",
        group="finance",
        default_chunk_days=31,
    ),
    "balancesheet_vip": DatasetSpec(
        name="balancesheet_vip",
        api_name="balancesheet_vip",
        layer="silver",
        primary_key=("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type", "update_flag"),
        strategy="paged_range",
        date_field="ann_date",
        group="finance",
        default_chunk_days=31,
    ),
    "cashflow_vip": DatasetSpec(
        name="cashflow_vip",
        api_name="cashflow_vip",
        layer="silver",
        primary_key=("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type", "update_flag"),
        strategy="paged_range",
        date_field="ann_date",
        group="finance",
        default_chunk_days=31,
    ),
    "fina_indicator_vip": DatasetSpec(
        name="fina_indicator_vip",
        api_name="fina_indicator_vip",
        layer="silver",
        primary_key=("ts_code", "ann_date", "end_date", "update_flag"),
        strategy="paged_range",
        date_field="ann_date",
        group="finance",
        default_chunk_days=31,
    ),
    "dividend": DatasetSpec(
        name="dividend",
        api_name="dividend",
        layer="silver",
        primary_key=("ts_code", "ann_date", "end_date", "div_proc"),
        strategy="daily",
        date_field="ann_date",
        group="corporate",
        default_chunk_days=1,
    ),
    "top10_holders": DatasetSpec(
        name="top10_holders",
        api_name="top10_holders",
        layer="silver",
        primary_key=("ts_code", "ann_date", "end_date", "holder_name"),
        strategy="paged_range",
        date_field="ann_date",
        group="corporate",
        default_chunk_days=31,
    ),
    "top10_floatholders": DatasetSpec(
        name="top10_floatholders",
        api_name="top10_floatholders",
        layer="silver",
        primary_key=("ts_code", "ann_date", "end_date", "holder_name"),
        strategy="paged_range",
        date_field="ann_date",
        group="corporate",
        default_chunk_days=31,
    ),
    "moneyflow": DatasetSpec(
        name="moneyflow",
        api_name="moneyflow",
        layer="silver",
        primary_key=("ts_code", "trade_date"),
        strategy="range",
        date_field="trade_date",
        group="market",
        default_chunk_days=1,
    ),
    "top_list": DatasetSpec(
        name="top_list",
        api_name="top_list",
        layer="silver",
        primary_key=("trade_date", "_row_no"),
        strategy="daily",
        date_field="trade_date",
        group="trading",
        default_chunk_days=1,
    ),
    "index_basic": DatasetSpec(
        name="index_basic",
        api_name="index_basic",
        layer="silver",
        primary_key=("ts_code",),
        strategy="param_sets",
        param_sets=(
            {"market": "SSE"},
            {"market": "SZSE"},
            {"market": "CSI"},
            {"market": "CICC"},
            {"market": "SW"},
            {"market": "OTH"},
        ),
        group="static",
    ),
    "index_daily": DatasetSpec(
        name="index_daily",
        api_name="index_daily",
        layer="silver",
        primary_key=("ts_code", "trade_date"),
        strategy="param_sets_range",
        date_field="trade_date",
        param_sets=(
            {"ts_code": "000001.SH"},
            {"ts_code": "399001.SZ"},
            {"ts_code": "399006.SZ"},
            {"ts_code": "000300.SH"},
            {"ts_code": "000905.SH"},
            {"ts_code": "000852.SH"},
            {"ts_code": "000985.CSI"},
        ),
        group="market",
        default_chunk_days=366,
    ),
    "index_weight": DatasetSpec(
        name="index_weight",
        api_name="index_weight",
        layer="silver",
        primary_key=("index_code", "con_code", "trade_date"),
        strategy="param_sets_range",
        date_field="trade_date",
        param_sets=(
            {"index_code": "000300.SH"},
            {"index_code": "000905.SH"},
            {"index_code": "000852.SH"},
            {"index_code": "000985.CSI"},
        ),
        group="benchmark",
        default_chunk_days=31,
    ),
    "index_classify": DatasetSpec(
        name="index_classify",
        api_name="index_classify",
        layer="silver",
        primary_key=("index_code", "industry_name", "level", "src"),
        strategy="param_sets",
        param_sets=(
            {"level": "L1", "src": "SW2021"},
            {"level": "L2", "src": "SW2021"},
            {"level": "L3", "src": "SW2021"},
        ),
        group="industry",
    ),
    "index_member_all": DatasetSpec(
        name="index_member_all",
        api_name="index_member_all",
        layer="silver",
        primary_key=("ts_code", "l1_code", "l2_code", "l3_code", "in_date", "out_date"),
        strategy="paged",
        group="industry",
    ),
}

TRADING_DAY_DATASETS = frozenset(
    {
        "daily",
        "daily_basic",
        "adj_factor",
        "stk_limit",
        "moneyflow",
        "top_list",
    }
)


SYNC_GROUPS: dict[str, tuple[str, ...]] = {
    "static": tuple(name for name, spec in DATASETS.items() if spec.group == "static"),
    "market": tuple(name for name, spec in DATASETS.items() if spec.group == "market"),
    "finance": tuple(name for name, spec in DATASETS.items() if spec.group == "finance"),
    "corporate": tuple(name for name, spec in DATASETS.items() if spec.group == "corporate"),
    "trading": tuple(name for name, spec in DATASETS.items() if spec.group == "trading"),
    "benchmark": tuple(name for name, spec in DATASETS.items() if spec.group == "benchmark"),
    "industry": tuple(name for name, spec in DATASETS.items() if spec.group == "industry"),
}
SYNC_GROUPS["all"] = tuple(DATASETS)


def get_dataset(name: str) -> DatasetSpec:
    try:
        return DATASETS[name]
    except KeyError as exc:
        raise DatasetNotFoundError(f"Unknown dataset: {name}") from exc


def enabled_dataset_names(configured: dict[str, bool]) -> list[str]:
    if not configured:
        return list(DATASETS)
    return [name for name in DATASETS if configured.get(name, False)]


def dataset_names_for_group(group: str) -> list[str]:
    try:
        return list(SYNC_GROUPS[group])
    except KeyError as exc:
        raise DatasetNotFoundError(f"Unknown dataset group: {group}") from exc
