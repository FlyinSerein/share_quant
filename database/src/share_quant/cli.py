from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

from .config import load_config, resolve_date
from .datasets import DATASETS, SYNC_GROUPS, enabled_dataset_names, get_dataset
from .phased_sync import PhasedSyncRunner
from .storage import StorageEngine, validate_all
from .sync import SyncEngine
from .tushare_adapter import TushareAdapter


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    storage = StorageEngine(config.data_root, config.duckdb_path)

    if args.command == "init-db":
        storage.init()
        print(f"Initialized database at {config.duckdb_path}")
        return 0

    if args.command == "status":
        storage.init()
        with storage.connect() as con:
            rows = con.execute("select * from v_sync_status order by dataset").fetchall()
            if not rows:
                print("No sync status rows yet.")
            for row in rows:
                print(row)
        return 0

    if args.command == "validate":
        storage.init()
        failed = 0
        for dataset, status, detail in validate_all(storage):
            print(f"{dataset}\t{status}\t{detail}")
            failed += int(status != "passed")
        return 1 if failed else 0

    if args.command == "consolidate-bronze":
        storage.init()
        datasets = [args.dataset] if args.dataset else enabled_dataset_names(config.datasets)
        failed = 0
        for dataset in datasets:
            try:
                row_count = storage.consolidate_bronze(get_dataset(dataset))
            except Exception as exc:
                failed += 1
                print(f"{dataset}\tfailed\t{exc}")
            else:
                if row_count is None:
                    print(f"{dataset}\tskipped\tno bronze parquet files")
                else:
                    print(f"{dataset}\tsuccess\trows={row_count}")
        if not failed:
            storage.create_views()
        return 1 if failed else 0

    if args.command == "repair-security-codes":
        storage.init()
        datasets = [args.dataset] if args.dataset else None
        results = storage.repair_security_code_aliases(datasets)
        if not results:
            print("No silver datasets with security-code columns were found.")
            return 0
        for dataset, (before, after, aliased) in results.items():
            print(
                f"{dataset}\tsuccess\tbefore={before}\tafter={after}"
                f"\taliased_rows={aliased}\tremoved={before - after}"
            )
        return 0

    if args.command in {"sync", "sync-all", "sync-phased"}:
        storage.init()
        start = resolve_date(args.start, config.default_start_date)
        end = resolve_date(args.end, config.default_end_date)
        tushare_config = config.tushare
        if args.command == "sync-phased" and args.rate_limit_seconds is not None:
            tushare_config = replace(config.tushare, rate_limit_seconds=args.rate_limit_seconds)
        if args.command == "sync-phased" and args.dry_run:
            adapter = None
            engine = SyncEngine(_UnavailableAdapter(), storage)
        else:
            if not os.getenv(config.tushare.token_env):
                print(f"Missing Tushare token. Set environment variable {config.tushare.token_env} before real sync.")
                return 1
            adapter = TushareAdapter(tushare_config)
            engine = SyncEngine(adapter, storage, write_silver=not getattr(args, "bronze_only", False))
        if args.command == "sync-phased":
            runner = PhasedSyncRunner(
                engine=engine,
                storage=storage,
                checkpoint_path=_catalog_path(config.data_root, args.checkpoint, "sync_phased_checkpoint.json"),
                progress_log_path=_catalog_path(config.data_root, args.progress_log, "sync_phased_progress.jsonl"),
            )
            summary = runner.run(
                groups=args.group or ["all"],
                enabled=config.datasets,
                start=start,
                end=end,
                skip_failures=not args.stop_on_failure,
                resume=not args.no_resume,
                pause_between_chunks=args.pause_between_chunks,
                dry_run=args.dry_run,
                create_views_on_finish=not args.bronze_only,
                checkpoint_every=args.checkpoint_every,
            )
            return 1 if summary.failed and args.stop_on_failure else 0
        if args.command == "sync":
            result = engine.sync_dataset(args.dataset, start, end)
            print(result)
        else:
            for result in engine.sync_all(config.datasets, start, end):
                print(result)
        storage.create_views()
        return 0

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="share-quant")
    parser.add_argument("--config", default=None, help="Path to YAML config.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init-db")
    sub.add_parser("status")
    sub.add_parser("validate")
    consolidate = sub.add_parser(
        "consolidate-bronze",
        help="Bulk-deduplicate bronze parquet files into silver after a bronze-only backfill.",
    )
    consolidate.add_argument("--dataset", choices=sorted(DATASETS), default=None)
    repair_codes = sub.add_parser(
        "repair-security-codes",
        help="Map historical stock-code aliases to current codes and deduplicate silver data.",
    )
    repair_codes.add_argument("--dataset", choices=sorted(DATASETS), default=None)

    sync = sub.add_parser("sync")
    sync.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    sync.add_argument("--start", default=None)
    sync.add_argument("--end", default=None)

    sync_all = sub.add_parser("sync-all")
    sync_all.add_argument("--start", default=None)
    sync_all.add_argument("--end", default=None)

    sync_phased = sub.add_parser("sync-phased")
    sync_phased.add_argument("--start", default=None)
    sync_phased.add_argument("--end", default=None)
    sync_phased.add_argument("--group", action="append", choices=sorted(SYNC_GROUPS), default=None)
    sync_phased.add_argument("--checkpoint", default=None)
    sync_phased.add_argument("--progress-log", default=None)
    sync_phased.add_argument("--rate-limit-seconds", type=float, default=None)
    sync_phased.add_argument("--pause-between-chunks", type=float, default=0.0)
    sync_phased.add_argument("--checkpoint-every", type=int, default=1)
    sync_phased.add_argument("--no-resume", action="store_true")
    sync_phased.add_argument("--stop-on-failure", action="store_true")
    sync_phased.add_argument("--dry-run", action="store_true")
    sync_phased.add_argument("--bronze-only", action="store_true", help="Only write raw bronze parquet chunks; skip silver upserts and view refresh.")

    return parser


class _UnavailableAdapter:
    def fetch(self, api_name: str, params: dict, fields: list[str] | None = None):  # pragma: no cover
        raise RuntimeError(f"Adapter is unavailable for dry-run command: {api_name}")


def _catalog_path(data_root: Path, value: str | None, default_name: str) -> Path:
    if value:
        return Path(value)
    return data_root / "catalog" / default_name


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
