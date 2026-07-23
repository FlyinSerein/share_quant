from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


SECURITY_CODE_ALIASES: dict[str, str] = {
    "000022.SZ": "001872.SZ",
    "000043.SZ": "001914.SZ",
    "300114.SZ": "302132.SZ",
}

SECURITY_ALIAS_RANK_COLUMN = "_security_alias_rank"

STOCK_CODE_COLUMNS_BY_DATASET: dict[str, tuple[str, ...]] = {
    "stock_basic": ("ts_code",),
    "namechange": ("ts_code",),
    "stock_company": ("ts_code",),
    "daily": ("ts_code",),
    "daily_basic": ("ts_code",),
    "adj_factor": ("ts_code",),
    "suspend_d": ("ts_code",),
    "stk_limit": ("ts_code",),
    "income_vip": ("ts_code",),
    "balancesheet_vip": ("ts_code",),
    "cashflow_vip": ("ts_code",),
    "fina_indicator_vip": ("ts_code",),
    "dividend": ("ts_code",),
    "top10_holders": ("ts_code",),
    "top10_floatholders": ("ts_code",),
    "moneyflow": ("ts_code",),
    "top_list": ("ts_code",),
    "index_weight": ("con_code",),
    "index_member_all": ("ts_code",),
}


def security_code_columns(dataset: str, available_columns: Iterable[str]) -> tuple[str, ...]:
    available = set(available_columns)
    return tuple(
        column
        for column in STOCK_CODE_COLUMNS_BY_DATASET.get(dataset, ())
        if column in available
    )


def canonicalize_security_codes(
    frame: pd.DataFrame,
    dataset: str,
    *,
    track_alias_source: bool = False,
) -> pd.DataFrame:
    columns = security_code_columns(dataset, frame.columns)
    if frame.empty or not columns:
        return frame

    prepared = frame.copy()
    alias_mask = pd.Series(False, index=prepared.index)
    for column in columns:
        alias_mask |= prepared[column].isin(SECURITY_CODE_ALIASES)
        prepared[column] = prepared[column].replace(SECURITY_CODE_ALIASES)
    if track_alias_source:
        prepared[SECURITY_ALIAS_RANK_COLUMN] = alias_mask.astype("int8")
    return prepared
