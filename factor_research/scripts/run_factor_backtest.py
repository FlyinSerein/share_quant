from __future__ import annotations

import argparse
from pathlib import Path

from factor_research.factor_backtest import FactorResearchRunner
from factor_research.paths import load_paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Excel single-factor Top20% monthly backtests.")
    parser.add_argument("--start", default="2022-01-01", help="Evaluation start date, YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="Evaluation end date, YYYY-MM-DD. Defaults to latest local trade date.")
    parser.add_argument("--warmup-start", default="2021-01-01", help="Warmup start date for rolling factors.")
    parser.add_argument("--benchmark", default="000985.CSI", help="Benchmark index code.")
    parser.add_argument("--transaction-cost", type=float, default=0.001, help="One-way transaction cost.")
    parser.add_argument("--output-dir", default=None, help="Single-factor output directory. Defaults to outputs/single_factor/.")
    parser.add_argument(
        "--neutralized-subdir",
        default="neutralized",
        help="Neutralized stage name under output_root, or a subdirectory under a custom output-dir.",
    )
    args = parser.parse_args()

    paths_config = load_paths()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else paths_config.output_root / "single_factor"
    neutralized_output_dir = None if args.output_dir else paths_config.output_root / args.neutralized_subdir
    runner = FactorResearchRunner(
        project_root=paths_config.database_root,
        db_path=paths_config.database_path,
        output_dir=output_dir,
        start=args.start,
        end=args.end,
        warmup_start=args.warmup_start,
        benchmark=args.benchmark,
        transaction_cost=args.transaction_cost,
        neutralized_subdir=args.neutralized_subdir,
        neutralized_output_dir=neutralized_output_dir,
    )
    paths = runner.run()
    print("Factor backtest finished.")
    for key, path in sorted(paths.items()):
        print(f"{key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
