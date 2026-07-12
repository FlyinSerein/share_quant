from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import pandas as pd

from .datasets import DatasetSpec, enabled_dataset_names, get_dataset
from .storage import StorageEngine
from .utils import compact_date


class DataAdapter(Protocol):
    def fetch(self, api_name: str, params: dict, fields: list[str] | None = None) -> pd.DataFrame:
        ...


@dataclass
class SyncResult:
    dataset: str
    batch_id: str
    status: str
    rows_fetched: int
    rows_stored: int


class SyncEngine:
    def __init__(self, adapter: DataAdapter, storage: StorageEngine, write_silver: bool = True):
        self.adapter = adapter
        self.storage = storage
        self.write_silver = write_silver

    def sync_dataset(self, dataset: str, start: str | None = None, end: str | None = None) -> SyncResult:
        spec = get_dataset(dataset)
        params = self._params_for(spec, start, end)
        batch_id = f"{spec.name}-{uuid.uuid4().hex[:12]}"
        self.storage.record_batch_start(batch_id, spec, params)
        try:
            frame = self._fetch_for_spec(spec, start, end, params)
            frame = self._prepare_frame(spec, frame)
            rows_fetched = len(frame)
            if rows_fetched:
                self.storage.write_bronze(spec, frame, batch_id, params)
                rows_stored = self.storage.upsert_silver(spec, frame, batch_id) if self.write_silver else rows_fetched
            else:
                rows_stored = 0
            self.storage.record_batch_finish(batch_id, spec, "success", rows_stored, start, end)
            return SyncResult(spec.name, batch_id, "success", rows_fetched, rows_stored)
        except Exception as exc:
            self.storage.record_batch_finish(batch_id, spec, "failed", 0, start, end, str(exc))
            raise

    def sync_all(self, enabled: dict[str, bool], start: str | None = None, end: str | None = None) -> list[SyncResult]:
        results = []
        for name in enabled_dataset_names(enabled):
            results.append(self.sync_dataset(name, start, end))
        return results

    def _params_for(self, spec: DatasetSpec, start: str | None, end: str | None) -> dict:
        params = dict(spec.default_params)
        if spec.strategy in {"static", "param_sets"}:
            return params
        if spec.strategy == "paged" and not spec.date_field:
            return params
        start_compact = compact_date(start) if start else None
        end_compact = compact_date(end) if end else None
        if start_compact:
            params["start_date"] = start_compact
        if end_compact:
            params["end_date"] = end_compact
        return params

    def _fetch_for_spec(
        self,
        spec: DatasetSpec,
        start: str | None,
        end: str | None,
        params: dict,
    ) -> pd.DataFrame:
        if spec.strategy == "param_sets":
            return self._fetch_param_sets(spec, params)
        if spec.strategy == "param_sets_range":
            return self._fetch_param_sets(spec, params)
        if spec.strategy == "paged":
            return self._fetch_paged(spec, params)
        if spec.strategy != "daily":
            return self.adapter.fetch(spec.api_name, params=params, fields=list(spec.fields) or None)

        frames = []
        for current in _iter_dates(start, end):
            daily_params = dict(spec.default_params)
            daily_params[spec.date_field or "trade_date"] = compact_date(current)
            frame = self.adapter.fetch(spec.api_name, params=daily_params, fields=list(spec.fields) or None)
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def _fetch_param_sets(self, spec: DatasetSpec, params: dict) -> pd.DataFrame:
        frames = []
        param_sets = spec.param_sets or (spec.default_params,)
        for param_set in param_sets:
            fetch_params = dict(params)
            fetch_params.update(param_set)
            frame = self.adapter.fetch(spec.api_name, params=fetch_params, fields=list(spec.fields) or None)
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def _fetch_paged(self, spec: DatasetSpec, params: dict) -> pd.DataFrame:
        frames = []
        limit = 3000
        offset = 0
        while True:
            page_params = dict(params)
            page_params.update({"limit": limit, "offset": offset})
            frame = self.adapter.fetch(spec.api_name, params=page_params, fields=list(spec.fields) or None)
            if frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def _prepare_frame(self, spec: DatasetSpec, frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty or "_row_no" not in spec.primary_key:
            return frame
        prepared = frame.copy()
        date_col = spec.date_field
        if date_col and date_col in prepared.columns:
            prepared["_row_no"] = prepared.groupby(date_col, sort=False).cumcount() + 1
        else:
            prepared["_row_no"] = range(1, len(prepared) + 1)
        return prepared


def _iter_dates(start: str | None, end: str | None) -> list[str]:
    if not start and not end:
        raise ValueError("daily strategy requires at least one date")
    start_dt = datetime.strptime((start or end or ""), "%Y-%m-%d")
    end_dt = datetime.strptime((end or start or ""), "%Y-%m-%d")
    if end_dt < start_dt:
        raise ValueError("end date must be greater than or equal to start date")
    values = []
    current = start_dt
    while current <= end_dt:
        values.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return values
