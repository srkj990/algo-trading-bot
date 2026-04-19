from datetime import time

from engines.common import build_position, evaluate_exit, get_symbol_deployed_capital
from executor import get_delivery_holdings
from logger import log_event


class DeliveryEquityEngine:
    name = "delivery_equity"
    data_period = "6mo"
    data_interval = "1d"
    order_product = "CNC"
    supported_strategies = {
        "1": "MA",
        "2": "RSI",
        "3": "BREAKOUT",
    }
    market_open = time(9, 15)
    market_close = time(15, 30)
    sleep_seconds = 300
    cooldown_seconds = 0

    def __init__(self, sl_percent, target_percent, trailing_percent):
        self.sl_percent = sl_percent
        self.target_percent = target_percent
        self.trailing_percent = trailing_percent
        self.max_symbol_allocation = 0.25

    def get_cycle_state(self, now):
        if now.weekday() >= 5:
            return {
                "manage_positions": False,
                "allow_entries": False,
                "force_square_off": False,
                "allow_scan": False,
                "reason": "Weekend - market closed",
            }

        current_time = now.time()
        if current_time < self.market_open or current_time >= self.market_close:
            return {
                "manage_positions": False,
                "allow_entries": False,
                "force_square_off": False,
                "allow_scan": False,
                "reason": "Market closed for delivery execution",
            }

        return {
            "manage_positions": True,
            "allow_entries": True,
            "force_square_off": False,
            "allow_scan": True,
            "reason": "Delivery session active",
        }

    def normalize_entry_signal(self, signal):
        if signal == "BUY":
            return "BUY"
        return None

    def set_portfolio_rules(self, max_symbol_allocation):
        self.max_symbol_allocation = max_symbol_allocation

    def evaluate_position_exit(self, position, latest_candle):
        return evaluate_exit(position, latest_candle, include_target=False)

    def get_signal_exit_reason(self, position, signal):
        if position["side"] == "BUY" and signal == "SELL":
            return "SELL_SIGNAL"
        return None

    def apply_entry_allocation_limit(
        self,
        symbol,
        quantity,
        entry_price,
        positions,
        capital,
    ):
        max_symbol_capital = capital * self.max_symbol_allocation
        current_symbol_capital = get_symbol_deployed_capital(positions, symbol)
        remaining_symbol_capital = max(0.0, max_symbol_capital - current_symbol_capital)
        symbol_cap_qty = int(remaining_symbol_capital / entry_price) if entry_price > 0 else 0
        final_qty = min(quantity, symbol_cap_qty)

        log_event(
            (
                f"[DELIVERY] Symbol allocation for {symbol}: "
                f"Current={current_symbol_capital:.2f}, "
                f"Max={max_symbol_capital:.2f}, "
                f"Remaining={remaining_symbol_capital:.2f}, "
                f"Qty after allocation={final_qty}"
            )
        )

        return final_qty

    def reconcile_startup(self, execution_mode, persisted_positions):
        if execution_mode != "LIVE":
            log_event(
                f"[RECON] {self.name} running in paper mode - using persisted positions"
            )
            return persisted_positions

        broker_positions = {}
        for item in get_delivery_holdings():
            quantity = int(item.get("quantity", 0)) + int(item.get("t1_quantity", 0))
            if quantity <= 0:
                continue

            symbol = f"{item['tradingsymbol']}.NS"
            broker_positions[symbol] = build_position(
                symbol=symbol,
                side="BUY",
                quantity=quantity,
                entry_price=float(item.get("average_price") or item.get("last_price") or 0),
                sl_pct=self.sl_percent,
                target_pct=self.target_percent,
                trailing_pct=self.trailing_percent,
            )

        if broker_positions:
            log_event(
                f"[RECON] Loaded {len(broker_positions)} live delivery holdings from broker"
            )
            return broker_positions

        log_event("[RECON] No live delivery holdings at broker startup sync")
        return {}
