from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd

from .config import TushareConfig
from .errors import MissingTokenError


@dataclass
class TushareAdapter:
    config: TushareConfig
    token: str | None = None

    def __post_init__(self) -> None:
        self.token = self.token or os.getenv(self.config.token_env)
        self._client: Any | None = None

    def fetch(self, api_name: str, params: dict[str, Any] | None = None, fields: list[str] | None = None) -> pd.DataFrame:
        client = self._get_client()
        params = params or {}
        last_exc: Exception | None = None
        fields_arg = ",".join(fields) if fields else None

        for attempt in range(1, self.config.max_retries + 1):
            try:
                if self.config.rate_limit_seconds:
                    time.sleep(self.config.rate_limit_seconds)
                result = client.query(api_name, fields=fields_arg, **params)
                if result is None:
                    return pd.DataFrame()
                return result
            except Exception as exc:  # pragma: no cover - real API behavior.
                last_exc = exc
                if attempt >= self.config.max_retries:
                    break
                time.sleep(self.config.retry_backoff_seconds * attempt)

        raise RuntimeError(f"Tushare API {api_name!r} failed after retries: {last_exc}") from last_exc

    def _get_client(self) -> Any:
        if not self.token:
            raise MissingTokenError(
                f"Missing Tushare token. Set environment variable {self.config.token_env} before real sync."
            )
        if self._client is None:
            import tushare as ts

            self._client = ts.pro_api(self.token)
        return self._client

