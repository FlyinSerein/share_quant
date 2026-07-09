from __future__ import annotations

from datetime import UTC, date, datetime


def compact_date(value: str | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    cleaned = value.replace("-", "")
    if len(cleaned) != 8 or not cleaned.isdigit():
        raise ValueError(f"Expected YYYY-MM-DD or YYYYMMDD date, got {value!r}")
    return cleaned


def iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
