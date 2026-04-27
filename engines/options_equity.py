from datetime import time

from engines.base import TradingEngine
from engines.common import (
    build_position,
    evaluate_exit,
    get_symbol_deployed_capital,
    merge_persisted_position_state,
)
from executor_fno import get_options_positions
from fno_data_fetcher import get_contract_lot_size
from logger import log_event


class OptionsEquityEngine(TradingEngine):
    name = "options_equity"
    data_period = "2mo"
    data_interval = "15m"
    order_product = "NRML"
    supported_strategies = {
        "1": "MA",
        "2": "RSI",
        "3": "BREAKOUT",
        "4": "VWAP",
        "5": "ORB",
    }
    market_open = time(9, 15)
    market_close = time(15, 20)
    sleep_seconds = 60
    cooldown_seconds = 300
    max_symbol_allocation = 0.25

    def __init__(self, sl_percent, target_percent, trailing_percent):
        self.sl_percent = sl_percent
        self.target_percent = target_percent
        self.trailing_percent = trailing_percent

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
                "reason": "Market closed for options trading",
            }

        return {
            "manage_positions": True,
            "allow_entries": True,
            "force_square_off": False,
            "allow_scan": True,
            "reason": "Options session active",
        }

    def normalize_entry_signal(self, signal):
        return signal if signal in {"BUY", "SELL"} else None

    def evaluate_position_exit(self, position, latest_candle):
        return evaluate_exit(position, latest_candle, include_target=True)

    def get_signal_exit_reason(self, position, signal):
        if signal in {"BUY", "SELL"} and signal != position["side"]:
            return "REVERSAL"
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
        lot_size = get_contract_lot_size(symbol)
        capped_quantity = min(quantity, symbol_cap_qty)
        return (capped_quantity // lot_size) * lot_size

    def reconcile_startup(self, execution_mode, persisted_positions):
        if execution_mode != "LIVE":
            log_event(
                f"[RECON] {self.name} running in paper mode - using persisted positions"
            )
            return persisted_positions

        try:
            broker_positions = {}
            for item in get_options_positions():
                tradingsymbol = item.get("tradingsymbol") or item.get("symbol")
                exchange = (item.get("exchange") or "NFO").upper()
                symbol = (
                    f"{exchange}:{tradingsymbol}"
                    if tradingsymbol and ":" not in tradingsymbol
                    else tradingsymbol
                )
                if not symbol:
                    continue
                quantity = int(item.get("quantity") or 0)
                if quantity == 0:
                    continue

                broker_position = build_position(
                    symbol=symbol,
                    side="BUY" if quantity > 0 else "SELL",
                    quantity=abs(quantity),
                    entry_price=float(item.get("average_price") or 0),
                    sl_pct=self.sl_percent,
                    target_pct=self.target_percent,
                    trailing_pct=self.trailing_percent,
                    lot_size=get_contract_lot_size(symbol),
                )
                broker_positions[symbol] = merge_persisted_position_state(
                    broker_position,
                    persisted_positions.get(symbol),
                )

            log_event(
                f"[RECON] Loaded {len(broker_positions)} live options holdings from broker"
            )
            return broker_positions
        except NotImplementedError as ex:
            log_event(f"[RECON] Options startup sync unavailable: {ex}", "warning")
            return persisted_positions
