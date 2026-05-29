from datetime import datetime, time

import pandas as pd

from config import ENGINE_DEFAULTS
from engines.common import build_position, evaluate_exit, get_symbol_deployed_capital
from engines.base import TradingEngine
from executor import get_delivery_holdings
from logger import log_event
from risk_manager import calculate_target_price


class DeliveryEquityEngine(TradingEngine):
    settings = ENGINE_DEFAULTS["delivery_equity"]
    name = "delivery_equity"
    data_period = str(settings.get("data_period", "6mo"))
    data_interval = str(settings.get("data_interval", "1d"))
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
    sleep_seconds = int(settings.get("sleep_seconds", 300))
    cooldown_seconds = int(settings.get("cooldown_seconds", 0))
    _adaptive_stop = {
        "SIDEWAYS": float(settings.get("adaptive_stop_multiplier_sideways", 2.4)),
        "NORMAL": float(settings.get("adaptive_stop_multiplier_normal", 2.0)),
        "EXPANSION": float(settings.get("adaptive_stop_multiplier_expansion", 1.8)),
    }
    _adaptive_target = {
        "SIDEWAYS": float(settings.get("adaptive_target_multiplier_sideways", 1.6)),
        "NORMAL": float(settings.get("adaptive_target_multiplier_normal", 2.6)),
        "EXPANSION": float(settings.get("adaptive_target_multiplier_expansion", 3.4)),
    }
    _adaptive_trailing = {
        "SIDEWAYS": float(settings.get("adaptive_trailing_multiplier_sideways", 1.2)),
        "NORMAL": float(settings.get("adaptive_trailing_multiplier_normal", 1.5)),
        "EXPANSION": float(settings.get("adaptive_trailing_multiplier_expansion", 1.8)),
    }
    _adaptive_min_stop_pct = float(settings.get("adaptive_min_stop_pct", 0.018))
    _adaptive_min_target_pct = float(settings.get("adaptive_min_target_pct", 0.040))
    _adaptive_min_trailing_pct = float(settings.get("adaptive_min_trailing_pct", 0.012))
    _adaptive_conviction_score_weight = float(settings.get("adaptive_conviction_score_weight", 0.75))
    _volatility_trailing_range_multiplier = float(settings.get("volatility_trailing_range_multiplier", 1.2))

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

    @staticmethod
    def _adaptive_regime_label(signal_score, swing_range_distance, entry_price, atr):
        entry_price = max(float(entry_price or 0.0), 0.01)
        score = min(1.0, max(0.0, float(signal_score or 0.0)))
        range_ratio = float(swing_range_distance or 0.0) / entry_price
        atr_ratio = float(atr or 0.0) / entry_price
        if score >= 0.035 or range_ratio >= 0.035 or atr_ratio >= 0.025:
            return "EXPANSION"
        if score <= 0.010 and range_ratio <= 0.015 and atr_ratio <= 0.012:
            return "SIDEWAYS"
        return "NORMAL"

    def get_equity_volatility_trailing_distance(self, daily_df):
        if daily_df is None or daily_df.empty:
            return 0.0
        lookback = min(len(daily_df), 10)
        recent = daily_df.tail(max(3, lookback))
        recent_ranges = pd.to_numeric(recent["High"], errors="coerce") - pd.to_numeric(
            recent["Low"],
            errors="coerce",
        )
        avg_range = float(recent_ranges.dropna().mean()) if not recent_ranges.empty else 0.0
        if avg_range != avg_range:
            return 0.0
        return max(0.0, avg_range * self._volatility_trailing_range_multiplier)

    def get_trend_adaptive_level_spec(
        self,
        *,
        entry_price,
        side,
        atr,
        signal_score,
        analytics=None,
        volatility_distance=None,
    ):
        del analytics
        regime = self._adaptive_regime_label(
            signal_score,
            volatility_distance,
            entry_price,
            atr,
        )
        base_atr = max(float(atr or 0.0), float(entry_price) * 0.006)
        normalized_score = min(1.0, max(0.0, float(signal_score or 0.0)))
        conviction = 1.0 + (normalized_score * self._adaptive_conviction_score_weight)

        stop_multiplier = self._adaptive_stop[regime]
        target_multiplier = self._adaptive_target[regime]
        trailing_multiplier = self._adaptive_trailing[regime]

        stop_distance = max(float(entry_price) * self._adaptive_min_stop_pct, base_atr * stop_multiplier)
        target_distance = max(
            float(entry_price) * self._adaptive_min_target_pct,
            base_atr * target_multiplier * conviction,
        )
        atr_trailing_distance = max(
            float(entry_price) * self._adaptive_min_trailing_pct,
            base_atr * trailing_multiplier,
        )
        swing_volatility_distance = max(float(volatility_distance or 0.0), 0.0)
        trailing_distance = max(atr_trailing_distance, swing_volatility_distance)
        level1_distance = target_distance * 0.45
        level2_distance = target_distance
        level3_distance = target_distance * 1.5
        stop_loss_price = (
            float(entry_price) - stop_distance
            if side == "BUY"
            else float(entry_price) + stop_distance
        )
        return {
            "adaptive_regime": regime,
            "adaptive_signal_score": normalized_score,
            "stop_distance": stop_distance,
            "stop_loss_price": max(0.01, stop_loss_price),
            "atr_trailing_distance": atr_trailing_distance,
            "swing_volatility_distance": swing_volatility_distance,
            "trailing_distance": trailing_distance,
            "level1_target": max(
                0.01,
                calculate_target_price(side, float(entry_price), level1_distance),
            ),
            "level2_target": max(
                0.01,
                calculate_target_price(side, float(entry_price), level2_distance),
            ),
            "level3_target": max(
                0.01,
                calculate_target_price(side, float(entry_price), level3_distance),
            ),
            "trailing_activation_distance": max(
                float(trailing_distance),
                float(level1_distance),
            ),
        }

    def build_trend_adaptive_position(
        self,
        *,
        symbol,
        side,
        quantity,
        entry_price,
        atr,
        signal_score,
        now,
        engine_name,
        execution_mode,
        order_product,
        volatility_distance=None,
        extra_fields=None,
    ):
        level_spec = self.get_trend_adaptive_level_spec(
            entry_price=entry_price,
            side=side,
            atr=atr,
            signal_score=signal_score,
            volatility_distance=volatility_distance,
        )
        payload = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entry_price": float(entry_price),
            "stop_loss": level_spec["stop_loss_price"],
            "target": level_spec["level3_target"],
            "trailing_stop": level_spec["stop_loss_price"],
            "trailing_distance": level_spec["trailing_distance"],
            "trailing_activation_distance": level_spec["trailing_activation_distance"],
            "trailing_active": False,
            "atr": atr,
            "stop_distance": level_spec["stop_distance"],
            "entry_time": now.isoformat(),
            "engine_name": engine_name,
            "execution_mode": execution_mode,
            "order_product": order_product,
            "adaptive_levels_enabled": True,
            "adaptive_horizon": "SWING",
            "adaptive_regime": level_spec["adaptive_regime"],
            "adaptive_signal_score": level_spec["adaptive_signal_score"],
            "adaptive_level1_target": level_spec["level1_target"],
            "adaptive_level2_target": level_spec["level2_target"],
            "adaptive_level3_target": level_spec["level3_target"],
            "swing_volatility_distance": level_spec["swing_volatility_distance"],
            "atr_trailing_distance": level_spec["atr_trailing_distance"],
        }
        if extra_fields:
            payload.update(extra_fields)
        return build_position(**payload)

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
