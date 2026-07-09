from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from share_quant.cli import main


class CliTest(unittest.TestCase):
    def test_init_db_creates_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(
                f"""
data_root: {Path(tmp).as_posix()}/data
duckdb_path: {Path(tmp).as_posix()}/data/share_quant.duckdb
default_start_date: "2021-01-01"
default_end_date: today
tushare:
  token_env: TUSHARE_TOKEN
datasets:
  daily: true
""".strip(),
                encoding="utf-8",
            )

            exit_code = main(["--config", str(config), "init-db"])

            self.assertEqual(exit_code, 0)
            self.assertTrue((Path(tmp) / "data" / "share_quant.duckdb").exists())

    def test_sync_phased_dry_run_plans_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            checkpoint = Path(tmp) / "checkpoint.json"
            progress_log = Path(tmp) / "progress.jsonl"
            config.write_text(
                f"""
data_root: {Path(tmp).as_posix()}/data
duckdb_path: {Path(tmp).as_posix()}/data/share_quant.duckdb
default_start_date: "2021-01-01"
default_end_date: today
tushare:
  token_env: TUSHARE_TOKEN
datasets:
  stock_basic: true
  daily: true
""".strip(),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "--config",
                    str(config),
                    "sync-phased",
                    "--group",
                    "static",
                    "--start",
                    "2021-01-01",
                    "--end",
                    "2021-01-02",
                    "--checkpoint",
                    str(checkpoint),
                    "--progress-log",
                    str(progress_log),
                    "--dry-run",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(progress_log.exists())

    def test_validate_returns_nonzero_for_missing_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(
                f"""
data_root: {Path(tmp).as_posix()}/data
duckdb_path: {Path(tmp).as_posix()}/data/share_quant.duckdb
default_start_date: "2021-01-01"
default_end_date: today
tushare:
  token_env: TUSHARE_TOKEN
datasets:
  daily: true
""".strip(),
                encoding="utf-8",
            )

            exit_code = main(["--config", str(config), "validate"])

            self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
