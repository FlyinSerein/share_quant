from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .datasets import DatasetSpec, dataset_names_for_group, get_dataset
from .storage import StorageEngine
from .sync import SyncEngine
from .utils import iso_now


ProgressLogger = Callable[[str], None]


@dataclass(frozen=True)
class SyncChunk:
    dataset: str
    group: str
    start: str | None
    end: str | None

    @property
    def key(self) -> str:
        return f"{self.dataset}:{self.start or '-'}:{self.end or '-'}"


@dataclass
class PhasedSyncSummary:
    planned: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0


class PhasedSyncRunner:
    def __init__(
        self,
        engine: SyncEngine,
        storage: StorageEngine,
        checkpoint_path: Path,
        progress_log_path: Path,
        logger: ProgressLogger = print,
    ) -> None:
        self.engine = engine
        self.storage = storage
        self.checkpoint_path = Path(checkpoint_path)
        self.progress_log_path = Path(progress_log_path)
        self.logger = logger

    def run(
        self,
        groups: list[str],
        enabled: dict[str, bool],
        start: str,
        end: str,
        skip_failures: bool = True,
        resume: bool = True,
        pause_between_chunks: float = 0.0,
        dry_run: bool = False,
        create_views_on_finish: bool = True,
    ) -> PhasedSyncSummary:
        checkpoint = self._load_checkpoint() if resume else {"completed_chunks": {}}
        completed = checkpoint.setdefault("completed_chunks", {})
        chunks = list(plan_chunks(groups, enabled, start, end))
        summary = PhasedSyncSummary(planned=len(chunks))
        self._emit("run_start", None, planned=summary.planned, groups=groups, start=start, end=end, dry_run=dry_run)

        for index, chunk in enumerate(chunks, start=1):
            if resume and chunk.key in completed:
                summary.skipped += 1
                self._emit("skip_completed", chunk, index=index, total=summary.planned)
                continue

            if dry_run:
                self._emit("dry_run", chunk, index=index, total=summary.planned)
                continue

            self._emit("chunk_start", chunk, index=index, total=summary.planned)
            try:
                result = self.engine.sync_dataset(chunk.dataset, chunk.start, chunk.end)
            except Exception as exc:
                summary.failed += 1
                self._emit("chunk_failed", chunk, index=index, total=summary.planned, error=str(exc))
                if not skip_failures:
                    raise
            else:
                summary.succeeded += 1
                completed[chunk.key] = {
                    "dataset": chunk.dataset,
                    "group": chunk.group,
                    "start": chunk.start,
                    "end": chunk.end,
                    "batch_id": result.batch_id,
                    "rows_fetched": result.rows_fetched,
                    "rows_stored": result.rows_stored,
                    "completed_at": iso_now(),
                }
                self._save_checkpoint(checkpoint)
                self._emit(
                    "chunk_success",
                    chunk,
                    index=index,
                    total=summary.planned,
                    batch_id=result.batch_id,
                    rows_fetched=result.rows_fetched,
                    rows_stored=result.rows_stored,
                )

            if pause_between_chunks > 0:
                time.sleep(pause_between_chunks)

        if not dry_run and create_views_on_finish:
            self.storage.create_views()
        self._emit("run_finish", None, planned=summary.planned, succeeded=summary.succeeded, skipped=summary.skipped, failed=summary.failed)
        return summary

    def _load_checkpoint(self) -> dict:
        if not self.checkpoint_path.exists():
            return {"completed_chunks": {}}
        with self.checkpoint_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save_checkpoint(self, checkpoint: dict) -> None:
        checkpoint["updated_at"] = iso_now()
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.checkpoint_path.with_suffix(self.checkpoint_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(checkpoint, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        tmp_path.replace(self.checkpoint_path)

    def _emit(self, event: str, chunk: SyncChunk | None, **extra: object) -> None:
        payload = {"ts": iso_now(), "event": event, **extra}
        if chunk is not None:
            payload.update({"dataset": chunk.dataset, "group": chunk.group, "start": chunk.start, "end": chunk.end})
        self.progress_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.progress_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        self.logger(_format_progress(payload))


def plan_chunks(groups: list[str], enabled: dict[str, bool], start: str, end: str) -> list[SyncChunk]:
    chunks: list[SyncChunk] = []
    seen: set[str] = set()
    for group in groups:
        for dataset in dataset_names_for_group(group):
            if dataset in seen or (enabled and not enabled.get(dataset, False)):
                continue
            seen.add(dataset)
            spec = get_dataset(dataset)
            chunks.extend(_chunks_for_spec(spec, start, end))
    return chunks


def _chunks_for_spec(spec: DatasetSpec, start: str, end: str) -> list[SyncChunk]:
    if not _uses_date_range(spec):
        return [SyncChunk(spec.name, spec.group, None, None)]

    chunk_days = spec.default_chunk_days or 31
    return [
        SyncChunk(spec.name, spec.group, chunk_start, chunk_end)
        for chunk_start, chunk_end in _iter_date_chunks(start, end, chunk_days)
    ]


def _uses_date_range(spec: DatasetSpec) -> bool:
    return bool(spec.date_field and spec.strategy not in {"static", "param_sets", "paged"})


def _iter_date_chunks(start: str, end: str, chunk_days: int) -> list[tuple[str, str]]:
    if chunk_days < 1:
        raise ValueError("chunk_days must be greater than 0")
    start_dt = datetime.strptime(start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end, "%Y-%m-%d").date()
    if end_dt < start_dt:
        raise ValueError("end date must be greater than or equal to start date")

    chunks = []
    current = start_dt
    while current <= end_dt:
        chunk_end = min(current + timedelta(days=chunk_days - 1), end_dt)
        chunks.append((current.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        current = chunk_end + timedelta(days=1)
    return chunks


def _format_progress(payload: dict) -> str:
    event = payload["event"]
    prefix = f"[{payload['ts']}] {event}"
    dataset = payload.get("dataset")
    if dataset:
        prefix += f" {dataset}"
    if payload.get("start") or payload.get("end"):
        prefix += f" {payload.get('start') or '-'}..{payload.get('end') or '-'}"
    if "index" in payload and "total" in payload:
        prefix += f" ({payload['index']}/{payload['total']})"
    if "rows_stored" in payload:
        prefix += f" rows_stored={payload['rows_stored']}"
    if "planned" in payload:
        prefix += f" planned={payload['planned']}"
    if "error" in payload:
        prefix += f" error={payload['error']}"
    return prefix
