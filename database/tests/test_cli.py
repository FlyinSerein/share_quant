from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from share_quant.cli import main
from share_quant.config import load_config


class CliTest(unittest.TestCase):
    def test_default_config_starts_in_2015(self) -> None:
        config = load_config()

        self.assertEqual(config.default_start_date, "2015-01-01")

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

    def test_sync_phased_dry_run_uses_2015_default_start_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            checkpoint = Path(tmp) / "checkpoint.json"
            progress_log = Path(tmp) / "progress.jsonl"
            config.write_text(
                f"""
data_root: {Path(tmp).as_posix()}/data
duckdb_path: {Path(tmp).as_posix()}/data/share_quant.duckdb
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
                    "market",
                    "--end",
                    "2015-01-02",
                    "--checkpoint",
                    str(checkpoint),
                    "--progress-log",
                    str(progress_log),
                    "--checkpoint-every",
                    "100",
                    "--dry-run",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(progress_log.exists())
            first_event = json.loads(progress_log.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(first_event["start"], "2015-01-01")

    def test_consolidate_bronze_does_not_require_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(
                f"""
data_root: {Path(tmp).as_posix()}/data
duckdb_path: {Path(tmp).as_posix()}/data/share_quant.duckdb
tushare:
  token_env: TUSHARE_TOKEN
datasets:
  daily: true
""".strip(),
                encoding="utf-8",
            )

            exit_code = main(["--config", str(config), "consolidate-bronze"])

            self.assertEqual(exit_code, 0)

    def test_repair_security_codes_does_not_require_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(
                f"""
data_root: {Path(tmp).as_posix()}/data
duckdb_path: {Path(tmp).as_posix()}/data/share_quant.duckdb
tushare:
  token_env: TUSHARE_TOKEN
datasets:
  daily: true
""".strip(),
                encoding="utf-8",
            )

            exit_code = main(["--config", str(config), "repair-security-codes"])

            self.assertEqual(exit_code, 0)

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
