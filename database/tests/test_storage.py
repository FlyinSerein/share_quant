from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from share_quant.datasets import get_dataset
from share_quant.storage import StorageEngine, validate_usability


class StorageEngineTest(unittest.TestCase):
    def test_upsert_keeps_latest_batch_per_primary_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            spec = get_dataset("daily")

            first = pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "trade_date": "20210104", "close": 10.0},
                    {"ts_code": "000002.SZ", "trade_date": "20210104", "close": 20.0},
                ]
            )
            second = pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "trade_date": "20210104", "close": 11.0},
                ]
            )

            storage.upsert_silver(spec, first, "batch-1")
            count = storage.upsert_silver(spec, second, "batch-2")

            self.assertEqual(count, 2)
            with storage.connect() as con:
                rows = con.execute(
                    "select ts_code, trade_date, close from read_parquet(?) order by ts_code",
                    [str(storage.silver_path("daily"))],
                ).fetchall()
            self.assertEqual(rows, [("000001.SZ", "20210104", 11.0), ("000002.SZ", "20210104", 20.0)])

    def test_validate_reports_missing_primary_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            spec = get_dataset("daily")
            storage._write_parquet(pd.DataFrame([{"ts_code": "000001.SZ"}]), storage.silver_path("daily"))

            status, detail = storage.validate_dataset(spec)

            self.assertEqual(status, "failed")
            self.assertIn("trade_date", detail)

    def test_validate_passes_when_primary_key_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            spec = get_dataset("daily")
            storage._write_parquet(
                pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20210104"}]),
                storage.silver_path("daily"),
            )

            status, detail = storage.validate_dataset(spec)

            self.assertEqual(status, "passed")
            self.assertEqual(detail, "ok")

    def test_validate_reports_empty_silver_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            spec = get_dataset("daily")
            storage._write_parquet(
                pd.DataFrame(columns=["ts_code", "trade_date"]),
                storage.silver_path("daily"),
            )

            status, detail = storage.validate_dataset(spec)

            self.assertEqual(status, "failed")
            self.assertIn("empty", detail)

    def test_validate_reports_duplicate_primary_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            spec = get_dataset("daily")
            storage._write_parquet(
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "trade_date": "20210104", "close": 10.0},
                        {"ts_code": "000001.SZ", "trade_date": "20210104", "close": 11.0},
                    ]
                ),
                storage.silver_path("daily"),
            )

            status, detail = storage.validate_dataset(spec)

            self.assertEqual(status, "failed")
            self.assertIn("duplicate primary key", detail)

    def test_validate_reports_empty_primary_key_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            spec = get_dataset("daily")
            storage._write_parquet(
                pd.DataFrame([{"ts_code": "", "trade_date": "20210104", "close": 10.0}]),
                storage.silver_path("daily"),
            )

            status, detail = storage.validate_dataset(spec)

            self.assertEqual(status, "failed")
            self.assertIn("null or empty primary key", detail)

    def test_validate_reports_bad_date_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            spec = get_dataset("daily")
            storage._write_parquet(
                pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "2021-01-04", "close": 10.0}]),
                storage.silver_path("daily"),
            )

            status, detail = storage.validate_dataset(spec)

            self.assertEqual(status, "failed")
            self.assertIn("invalid trade_date format", detail)

    def test_usability_reports_daily_rows_without_adj_factor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            storage.upsert_silver(
                get_dataset("daily"),
                pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20210104", "close": 10.0}]),
                "batch-daily",
            )
            storage.upsert_silver(
                get_dataset("adj_factor"),
                pd.DataFrame([{"ts_code": "000002.SZ", "trade_date": "20210104", "adj_factor": 1.0}]),
                "batch-adj",
            )

            results = dict((name, (status, detail)) for name, status, detail in validate_usability(storage))

            self.assertEqual(results["cross:daily_adj_factor"][0], "failed")
            self.assertIn("daily rows without adj_factor", results["cross:daily_adj_factor"][1])

    def test_usability_reports_queryable_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()

            results = dict((name, (status, detail)) for name, status, detail in validate_usability(storage))

            self.assertEqual(results["view:v_adjusted_daily"][0], "passed")
            self.assertIn("queryable", results["view:v_adjusted_daily"][1])

    def test_adjusted_daily_view_uses_adj_factor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            storage.upsert_silver(
                get_dataset("daily"),
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "trade_date": "20210104", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0},
                        {"ts_code": "000001.SZ", "trade_date": "20210105", "open": 12.0, "high": 13.0, "low": 11.0, "close": 12.0},
                    ]
                ),
                "batch-daily",
            )
            storage.upsert_silver(
                get_dataset("adj_factor"),
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "trade_date": "20210104", "adj_factor": 2.0},
                        {"ts_code": "000001.SZ", "trade_date": "20210105", "adj_factor": 4.0},
                    ]
                ),
                "batch-adj",
            )
            storage.create_views()

            with storage.connect() as con:
                rows = con.execute(
                    """
                    select trade_date, close_hfq, close_qfq
                    from v_adjusted_daily
                    order by trade_date
                    """
                ).fetchall()
                returns = con.execute(
                    """
                    select trade_date, round(return_adjusted, 6)
                    from v_adjusted_returns
                    order by trade_date
                    """
                ).fetchall()

            self.assertEqual(rows, [("20210104", 20.0, 5.0), ("20210105", 48.0, 12.0)])
            self.assertEqual(returns, [("20210104", None), ("20210105", 1.4)])

    def test_stock_universe_view_tolerates_optional_stock_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageEngine(root / "data", root / "data" / "share_quant.duckdb")
            storage.init()
            storage.upsert_silver(
                get_dataset("daily"),
                pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20210104", "close": 10.0}]),
                "batch-daily",
            )
            storage.upsert_silver(
                get_dataset("stock_basic"),
                pd.DataFrame([{"ts_code": "000001.SZ", "name": "平安银行", "list_date": "19910403"}]),
                "batch-stock",
            )
            storage.create_views()

            with storage.connect() as con:
                row = con.execute(
                    """
                    select ts_code, trade_date, name, delist_date, is_listed_on_date, is_st_name
                    from v_stock_universe_daily
                    """
                ).fetchone()

            self.assertEqual(row, ("000001.SZ", "20210104", "平安银行", None, True, False))


if __name__ == "__main__":
    unittest.main()
