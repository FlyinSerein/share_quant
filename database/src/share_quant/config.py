from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared for normal use.
    yaml = None


DEFAULT_START_DATE = "2015-01-01"


@dataclass(frozen=True)
class TushareConfig:
    token_env: str = "TUSHARE_TOKEN"
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    rate_limit_seconds: float = 0.25


@dataclass(frozen=True)
class AppConfig:
    data_root: Path
    duckdb_path: Path
    default_start_date: str
    default_end_date: str
    tushare: TushareConfig
    datasets: dict[str, bool]


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return project_root() / "configs" / "default.yaml"


def resolve_date(value: str | None, default: str) -> str:
    raw = value or default
    if raw == "today":
        return date.today().strftime("%Y-%m-%d")
    return raw


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path else default_config_path()
    if yaml is None:
        raise RuntimeError("PyYAML is required to load YAML configuration.")
    with config_path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    root = project_root()
    data_root = _resolve_path(root, raw.get("data_root", "data"))
    duckdb_path = _resolve_path(root, raw.get("duckdb_path", data_root / "share_quant.duckdb"))
    ts_raw = raw.get("tushare", {}) or {}

    return AppConfig(
        data_root=data_root,
        duckdb_path=duckdb_path,
        default_start_date=str(raw.get("default_start_date", DEFAULT_START_DATE)),
        default_end_date=str(raw.get("default_end_date", "today")),
        tushare=TushareConfig(
            token_env=str(ts_raw.get("token_env", "TUSHARE_TOKEN")),
            max_retries=int(ts_raw.get("max_retries", 3)),
            retry_backoff_seconds=float(ts_raw.get("retry_backoff_seconds", 2)),
            rate_limit_seconds=float(ts_raw.get("rate_limit_seconds", 0.25)),
        ),
        datasets={str(k): bool(v) for k, v in (raw.get("datasets", {}) or {}).items()},
    )


def _resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path
