from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ResearchPaths:
    project_root: Path
    database_root: Path
    database_path: Path
    output_root: Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_paths(config_path: str | Path | None = None) -> ResearchPaths:
    root = project_root()
    path = Path(config_path).resolve() if config_path else root / "configs" / "default.yaml"
    with path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}

    database_path = _resolve(root, raw.get("database_path", "../database/data/share_quant.duckdb"))
    output_root = _resolve(root, raw.get("output_root", "outputs"))
    return ResearchPaths(
        project_root=root,
        database_root=database_path.parent.parent,
        database_path=database_path,
        output_root=output_root,
    )


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()
