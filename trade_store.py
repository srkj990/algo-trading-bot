from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from config import get_runtime_config
from models.trade_record import OrderAuditRecord, TradeRecord


class TradeStore:
    def __init__(
        self,
        engine_name: str,
        execution_mode: str,
        trade_day: datetime | None = None,
    ) -> None:
        runtime_config = get_runtime_config()
        self.config = runtime_config.trade_store
        self.engine_name = engine_name
        self.execution_mode = execution_mode
        self.trade_day = (trade_day or datetime.now()).date().isoformat()
        self.base_dir = Path(self.config.base_dir)

    def is_enabled(self) -> bool:
        if not self.config.enabled:
            return False
        if self.execution_mode == "PAPER" and not self.config.include_paper_trades:
            return False
        return True

    def _ensure_dir(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self, suffix: str) -> Path:
        return self.base_dir / f"{self.engine_name}_{self.trade_day}_{suffix}.jsonl"

    def _append(self, path: Path, payload: dict[str, Any]) -> None:
        self._ensure_dir()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True))
            handle.write("\n")

    def record_trade(self, trade: TradeRecord) -> None:
        if not self.is_enabled():
            return
        self._append(self._file_path("trades"), trade.to_dict())

    def record_order_audit(self, audit: OrderAuditRecord) -> None:
        if not self.is_enabled():
            return
        self._append(self._file_path("orders"), audit.to_dict())

    def load_trade_book(self) -> list[dict[str, Any]]:
        path = self._file_path("trades")
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        return rows
