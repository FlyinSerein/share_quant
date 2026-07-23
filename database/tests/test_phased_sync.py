from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from share_quant.phased_sync import PhasedSyncRunner, plan_chunks
from share_quant.storage import StorageEngine
from share_quant.sync import SyncResult


class _FakeSyncEngine:
    def sync_dataset(self, dataset: str, start: str | None, end: str | None) -> SyncResult:
        return SyncResult(dataset, f"batch-{start or 'static'}", "success", 0, 0)


class _CountingRunner(PhasedSyncRunner):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.checkpoint_saves = 0

    def _save_checkpoint(self, checkpoint: dict) -> None:
        self.checkpoint_saves += 1
        super()._save_checkpoint(checkpoint)


class PhasedSyncTest(unittest.TestCase):
    def test_plan_chunks_filters_trading_day_datasets(self) -> None:
        chunks = plan_chunks(
            groups=["market"],
            enabled={"daily": True, "trade_cal": True},
            start="2021-01-01",
            end="2021-01-03",
            open_trade_dates={"2021-01-01"},
        )

        daily_chunks = [chunk.key for chunk in chunks if chunk.dataset == "daily"]
        self.assertEqual(daily_chunks, ["daily:2021-01-01:2021-01-01"])
        self.assertEqual(
            [chunk.key for chunk in chunks if chunk.dataset == "trade_cal"],
            ["trade_cal:2021-01-01:2021-01-03"],
        )

    def test_runner_batches_checkpoint_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            runner = _CountingRunner(
                engine=_FakeSyncEngine(),
                storage=storage,
                checkpoint_path=root / "checkpoint.json",
                progress_log_path=root / "progress.jsonl",
                logger=lambda _message: None,
            )

            summary = runner.run(
                groups=["market"],
                enabled={"daily": True},
                start="2021-01-01",
                end="2021-01-03",
                create_views_on_finish=False,
                checkpoint_every=2,
            )

            self.assertEqual(summary.succeeded, 3)
            self.assertEqual(runner.checkpoint_saves, 2)

    def test_plan_chunks_uses_groups_and_dataset_chunk_size(self) -> None:
        chunks = plan_chunks(
            groups=["static", "market"],
            enabled={"stock_basic": True, "daily": True, "trade_cal": False},
            start="2021-01-01",
            end="2021-01-03",
        )

        self.assertEqual([chunk.key for chunk in chunks], [
            "stock_basic:-:-",
            "daily:2021-01-01:2021-01-01",
            "daily:2021-01-02:2021-01-02",
            "daily:2021-01-03:2021-01-03",
        ])


if __name__ == "__main__":
    unittest.main()
