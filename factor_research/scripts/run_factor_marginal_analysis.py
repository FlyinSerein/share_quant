from __future__ import annotations

from factor_research.factor_marginal_analysis import run_from_args


def main() -> int:
    paths = run_from_args()
    print("Factor marginal contribution analysis finished.")
    for key, path in sorted(paths.items()):
        print(f"{key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
