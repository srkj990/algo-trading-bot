from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4


def _timestamp() -> str:
    return datetime.now().isoformat()


@dataclass(slots=True)
class TradeRecord:
    symbol: str
    side: str
    quantity: int
    entry_time: str | None
    exit_time: str
    entry_price: float
    exit_price: float
    pnl: float
    estimated_charges: float
    net_pnl: float
    pnl_pct: float
    exit_reason: str
    engine_name: str | None = None
    execution_mode: str | None = None
    pair_id: str | None = None
    trade_id: str = field(default_factory=lambda: uuid4().hex)
    recorded_at: str = field(default_factory=_timestamp)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OrderAuditRecord:
    stage: str
    symbol: str
    side: str
    quantity: int
    product: str
    execution_mode: str
    provider: str
    status: str
    message: str | None = None
    order_id: str | None = None
    entry_price: float | None = None
    note: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    audit_id: str = field(default_factory=lambda: uuid4().hex)
    recorded_at: str = field(default_factory=_timestamp)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
