# Factor research project instructions

This project owns factor construction, diagnostics, backtests, portfolios, and
reports. It consumes the sibling database project through read-only DuckDB
connections.

- Never write to `../database/data/bronze`, `silver`, or `catalog`.
- Preserve signal timing, as-of joins, transaction-cost assumptions, and output schemas.
- Keep single-factor, neutralized, diagnostics, and multifactor outputs isolated.
- Run tests from this directory with `python -m pytest`.
