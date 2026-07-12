from __future__ import annotations

import argparse
from pathlib import Path

from factor_backtest import FactorResearchRunner


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Excel single-factor Top20% monthly backtests.")
    parser.add_argument("--start", default="2022-01-01", help="Evaluation start date, YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="Evaluation end date, YYYY-MM-DD. Defaults to latest local trade date.")
    parser.add_argument("--warmup-start", default="2021-01-01", help="Warmup start date for rolling factors.")
    parser.add_argument("--benchmark", default="000985.CSI", help="Benchmark index code.")
    parser.add_argument("--transaction-cost", type=float, default=0.001, help="One-way transaction cost.")
    parser.add_argument("--output-dir", default=None, help="Output directory. Defaults to this subproject's outputs/.")
    parser.add_argument(
        "--neutralized-subdir",
        default="neutralized",
        help="Subdirectory under output-dir for industry/size neutralized results.",
    )
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    project_root = here.parents[1]
    output_dir = Path(args.output_dir).resolve() if args.output_dir else here / "outputs"
    runner = FactorResearchRunner(
        project_root=project_root,
        db_path=project_root / "data" / "share_quant.duckdb",
        output_dir=output_dir,
        start=args.start,
        end=args.end,
        warmup_start=args.warmup_start,
        benchmark=args.benchmark,
        transaction_cost=args.transaction_cost,
        neutralized_subdir=args.neutralized_subdir,
    )
    paths = runner.run()
    print("Factor backtest finished.")
    for key, path in sorted(paths.items()):
        print(f"{key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
