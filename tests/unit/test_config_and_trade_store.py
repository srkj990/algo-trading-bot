from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from config import get_runtime_config
from models.trade_record import OrderAuditRecord, TradeRecord
from trade_store import TradeStore


class RuntimeConfigTests(unittest.TestCase):
    def test_runtime_config_validation_produces_sections(self) -> None:
        runtime_config = get_runtime_config()
        self.assertGreater(runtime_config.data_cache.max_entries, 0)
        self.assertIn("MIS", runtime_config.orders.allowed_products)
        self.assertIn("MA", runtime_config.strategy.min_candles)


class TradeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine_name = f"trade_store_test_{uuid.uuid4().hex}"
        self.store = TradeStore(self.engine_name, "PAPER")
        self.trade_path = self.store._file_path("trades")
        self.order_path = self.store._file_path("orders")
        self.addCleanup(self._cleanup_files)

    def _cleanup_files(self) -> None:
        for path in (self.trade_path, self.order_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        base_dir = Path(self.store.base_dir)
        try:
            if base_dir.exists() and not any(base_dir.iterdir()):
                base_dir.rmdir()
        except OSError:
            pass

    def test_record_trade_persists_jsonl_row(self) -> None:
        self.store.record_trade(
            TradeRecord(
                symbol="SBIN.NS",
                side="BUY",
                quantity=1,
                entry_time="2026-05-02T09:15:00",
                exit_time="2026-05-02T09:20:00",
                entry_price=100.0,
                exit_price=102.0,
                pnl=2.0,
                estimated_charges=0.5,
                net_pnl=1.5,
                pnl_pct=2.0,
                exit_reason="TARGET",
            )
        )
        self.assertEqual(len(self.store.load_trade_book()), 1)

    def test_record_order_audit_persists_jsonl_row(self) -> None:
        self.store.record_order_audit(
            OrderAuditRecord(
                stage="submitted",
                symbol="SBIN.NS",
                side="BUY",
                quantity=1,
                product="MIS",
                execution_mode="PAPER",
                provider="KITE",
                status="SKIPPED",
            )
        )
        self.assertTrue(self.order_path.exists())


if __name__ == "__main__":
    unittest.main()
