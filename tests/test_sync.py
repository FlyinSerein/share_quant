from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from share_quant.storage import StorageEngine
from share_quant.sync import SyncEngine


class FakeAdapter:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.calls: list[tuple[str, dict, list[str] | None]] = []

    def fetch(self, api_name: str, params: dict, fields: list[str] | None = None) -> pd.DataFrame:
        self.calls.append((api_name, params, fields))
        return self.frame.copy()


class PagedFakeAdapter:
    def __init__(self, frames: list[pd.DataFrame]):
        self.frames = frames
        self.calls: list[tuple[str, dict, list[str] | None]] = []

    def fetch(self, api_name: str, params: dict, fields: list[str] | None = None) -> pd.DataFrame:
        self.calls.append((api_name, params, fields))
        index = len(self.calls) - 1
        if index >= len(self.frames):
            return pd.DataFrame()
        return self.frames[index].copy()


class SyncEngineTest(unittest.TestCase):
    def test_sync_dataset_records_bronze_silver_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            adapter = FakeAdapter(
                pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20210104", "close": 10.0}])
            )
            engine = SyncEngine(adapter, storage)

            result = engine.sync_dataset("daily", "2021-01-01", "2021-01-31")

            self.assertEqual(result.status, "success")
            self.assertEqual(result.rows_fetched, 1)
            self.assertEqual(adapter.calls[0][0], "daily")
            self.assertEqual(adapter.calls[0][1]["start_date"], "20210101")
            self.assertEqual(adapter.calls[0][1]["end_date"], "20210131")
            self.assertTrue(storage.silver_path("daily").exists())
            with storage.connect() as con:
                status = con.execute(
                    "select status, row_count from sync_status where dataset = 'daily'"
                ).fetchone()
            self.assertEqual(status, ("success", 1))

    def test_sync_failure_is_recorded(self) -> None:
        class FailingAdapter:
            def fetch(self, api_name: str, params: dict, fields: list[str] | None = None) -> pd.DataFrame:
                raise RuntimeError("permission denied")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            engine = SyncEngine(FailingAdapter(), storage)

            with self.assertRaises(RuntimeError):
                engine.sync_dataset("daily", "2021-01-01", "2021-01-31")

            with storage.connect() as con:
                status = con.execute(
                    "select status from sync_status where dataset = 'daily'"
                ).fetchone()[0]
            self.assertEqual(status, "failed")

    def test_bronze_only_skips_silver_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            adapter = FakeAdapter(
                pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20210104", "close": 10.0}])
            )
            engine = SyncEngine(adapter, storage, write_silver=False)

            result = engine.sync_dataset("daily", "2021-01-04", "2021-01-04")

            self.assertEqual(result.rows_fetched, 1)
            self.assertEqual(result.rows_stored, 1)
            self.assertFalse(storage.silver_path("daily").exists())
            self.assertTrue((root / "data" / "bronze" / "daily").exists())

    def test_daily_strategy_uses_trade_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            adapter = FakeAdapter(
                pd.DataFrame(
                    [
                        {"trade_date": "20210104", "ts_code": "000001.SZ", "reason": "fixture"},
                        {"trade_date": "20210104", "ts_code": "000001.SZ", "reason": "fixture"},
                    ]
                )
            )
            engine = SyncEngine(adapter, storage)

            result = engine.sync_dataset("top_list", "2021-01-04", "2021-01-04")

            self.assertEqual(result.status, "success")
            self.assertEqual(adapter.calls[0][0], "top_list")
            self.assertEqual(adapter.calls[0][1], {"trade_date": "20210104"})
            self.assertEqual(result.rows_stored, 2)

    def test_paged_strategy_uses_limit_and_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            full_page = pd.DataFrame(
                [
                    {
                        "ts_code": f"000{i:03d}.SZ",
                        "name": "fixture",
                        "start_date": "20210104",
                        "end_date": None,
                        "ann_date": "20210104",
                        "change_reason": "fixture",
                    }
                    for i in range(3000)
                ]
            )
            last_page = pd.DataFrame(
                [
                    {
                        "ts_code": "600000.SH",
                        "name": "fixture",
                        "start_date": "20210105",
                        "end_date": None,
                        "ann_date": "20210105",
                        "change_reason": "fixture",
                    }
                ]
            )
            adapter = PagedFakeAdapter([full_page, last_page])
            engine = SyncEngine(adapter, storage)

            result = engine.sync_dataset("namechange")

            self.assertEqual(result.rows_fetched, 3001)
            self.assertEqual(adapter.calls[0][1]["limit"], 3000)
            self.assertEqual(adapter.calls[0][1]["offset"], 0)
            self.assertEqual(adapter.calls[1][1]["offset"], 3000)
            self.assertNotIn("start_date", adapter.calls[0][1])
            self.assertNotIn("end_date", adapter.calls[0][1])

    def test_param_sets_strategy_merges_static_calls_without_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            adapter = FakeAdapter(pd.DataFrame([{"ts_code": "000001.SZ"}]))
            engine = SyncEngine(adapter, storage)

            result = engine.sync_dataset("stock_basic", "2021-01-04", "2021-01-04")

            self.assertEqual(result.status, "success")
            self.assertEqual([call[1] for call in adapter.calls], [{"list_status": "L"}, {"list_status": "D"}, {"list_status": "P"}])
            self.assertIn("list_status", adapter.calls[0][2])
            self.assertIn("delist_date", adapter.calls[0][2])

    def test_param_sets_range_merges_params_with_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            adapter = FakeAdapter(
                pd.DataFrame([{"index_code": "000300.SH", "con_code": "000001.SZ", "trade_date": "20210104"}])
            )
            engine = SyncEngine(adapter, storage)

            result = engine.sync_dataset("index_weight", "2021-01-01", "2021-01-31")

            self.assertEqual(result.status, "success")
            self.assertEqual(adapter.calls[0][0], "index_weight")
            self.assertEqual(adapter.calls[0][1]["index_code"], "000300.SH")
            self.assertEqual(adapter.calls[0][1]["start_date"], "20210101")
            self.assertEqual(adapter.calls[0][1]["end_date"], "20210131")


if __name__ == "__main__":
    unittest.main()
