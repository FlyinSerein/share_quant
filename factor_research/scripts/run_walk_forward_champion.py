from __future__ import annotations

from factor_research.walk_forward_champion import run_from_args


def main() -> int:
    paths = run_from_args()
    print("Walk-forward champion comparison finished.")
    for key, path in sorted(paths.items()):
        print(f"{key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
