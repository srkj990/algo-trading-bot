from datetime import datetime, time

import pandas as pd

from config import ENGINE_DEFAULTS
from engines.common import build_position, evaluate_exit, get_symbol_deployed_capital
from engines.base import TradingEngine
from executor import get_delivery_holdings
from logger import log_event


class DeliveryEquityEngine(TradingEngine):
    settings = ENGINE_DEFAULTS["delivery_equity"]
    name = "delivery_equity"
    data_period = "6mo"
    data_interval = "1d"
    order_product = "CNC"
    nifty_trend_symbol = str(settings["nifty_trend_symbol"])
    nifty_trend_ma_window = int(settings["nifty_trend_ma_window"])
    max_hold_days = int(settings["max_hold_days"])
    supported_strategies = {
        "1": "MA",
        "2": "RSI",
        "3": "VWAP",
        "4": "BREAKOUT",
        "5": "ORB",
    }
    market_open = time(9, 15)
    market_close = time(15, 30)
    sleep_seconds = 300
    cooldown_seconds = 0

    def __init__(self, sl_percent, target_percent, trailing_percent):
        self.sl_percent = sl_percent
        self.target_percent = target_percent
        self.trailing_percent = trailing_percent
        self.max_symbol_allocation = float(self.settings["max_symbol_allocation"])

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

    def get_time_exit_reason(self, position, now):
        entry_time_raw = position.get("entry_time")
        if not entry_time_raw:
            return None
        try:
            entry_time = datetime.fromisoformat(str(entry_time_raw))
        except ValueError:
            return None
        held_business_days = max(0, len(pd.bdate_range(entry_time.date(), now.date())) - 1)
        if held_business_days >= self.max_hold_days:
            return "TIME_BASED"
        return None

    def get_trailing_activation_distance(self, entry_price, target_price, atr):
        entry_price = float(entry_price)
        target_price = float(target_price)
        atr = float(atr or 0.0)
        target_profit_distance = max(0.0, target_price - entry_price)
        profit_buffer_distance = max(0.0, atr * 0.5)
        return max(target_profit_distance, profit_buffer_distance, 0.01)

    def passes_nifty_trend_guard(self, index_history, now=None):
        if index_history is None or index_history.empty or "Close" not in index_history.columns:
            return False, "Nifty 50 trend guard unavailable (no index history)"

        history = index_history.sort_index()
        if now is not None:
            as_of = pd.Timestamp(now)
            if len(history.index) > 0:
                first_index = history.index[0]
                if getattr(first_index, "tzinfo", None) is not None and as_of.tzinfo is None:
                    as_of = as_of.tz_localize(first_index.tzinfo)
                elif getattr(first_index, "tzinfo", None) is None and as_of.tzinfo is not None:
                    as_of = as_of.tz_localize(None)
            history = history.loc[:as_of]

        closes = pd.to_numeric(history["Close"], errors="coerce").dropna()
        if len(closes) < self.nifty_trend_ma_window:
            return (
                False,
                f"Nifty 50 trend guard unavailable (need {self.nifty_trend_ma_window} daily closes)",
            )

        close = float(closes.iloc[-1])
        ma50 = float(closes.rolling(self.nifty_trend_ma_window).mean().iloc[-1])
        if close > ma50:
            return True, f"Nifty 50 uptrend confirmed: close {close:.2f} > 50DMA {ma50:.2f}"
        return False, f"Nifty 50 downtrend filter active: close {close:.2f} <= 50DMA {ma50:.2f}"

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

        # Import symbol tables and safety config
        from config import NIFTY50_SYMBOLS, MANUAL_SYMBOL_TABLE, SINGLE_SYMBOL_TABLE, ONLY_MANAGE_CONFIGURED_SYMBOLS

        broker_positions = {}
        for item in get_delivery_holdings():
            quantity = int(item.get("quantity", 0)) + int(item.get("t1_quantity", 0))
            if quantity <= 0:
                continue

            symbol = f"{item['tradingsymbol']}.NS"

            # SAFETY FILTER: Only manage positions in configured symbol tables (if enabled)
            if ONLY_MANAGE_CONFIGURED_SYMBOLS:
                allowed_symbols = set()
                for table in [NIFTY50_SYMBOLS, MANUAL_SYMBOL_TABLE.values(), SINGLE_SYMBOL_TABLE.values()]:
                    if isinstance(table, dict):
                        allowed_symbols.update(table.values())
                    else:
                        allowed_symbols.update(table)

                if symbol not in allowed_symbols:
                    log_event(
                        f"[RECON] Skipping {symbol} - not in configured symbol tables "
                        f"(set ONLY_MANAGE_CONFIGURED_SYMBOLS=False in config.py to manage all positions)"
                    )
                    continue

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
            filter_status = "filtered to configured symbols only" if ONLY_MANAGE_CONFIGURED_SYMBOLS else "all positions"
            log_event(
                f"[RECON] Loaded {len(broker_positions)} live delivery holdings from broker "
                f"({filter_status})"
            )
            return broker_positions

        log_event("[RECON] No live delivery holdings at broker startup sync")
        return {}
