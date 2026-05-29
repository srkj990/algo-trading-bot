from datetime import datetime, time, timedelta

from indicators import compute_vwap
from engines.base import TradingEngine
from engines.common import build_position, evaluate_exit
from executor import get_intraday_positions
from logger import log_event
from risk_manager import calculate_target_price
from config import (
    ENGINE_DEFAULTS,
    INTRADAY_EQUITY_AUTO_NORMAL_MIN_CONFIRMATIONS,
    INTRADAY_EQUITY_ENTRY_CUTOFF_MINUTES_BEFORE_SQUAREOFF,
    REVERSAL_EXIT_CONFIRMATION_CANDLES,
)


class IntradayEquityEngine(TradingEngine):
    settings = ENGINE_DEFAULTS["intraday_equity"]
    name = "intraday_equity"
    data_period = str(settings.get("data_period", "1d"))
    data_interval = str(settings.get("data_interval", "1m"))
    order_product = "MIS"
    supported_strategies = {
        "1": "MA",
        "2": "RSI",
        "3": "VWAP",
        "4": "BREAKOUT",
        "5": "ORB",
    }
    market_open = time(9, 15)
    square_off_time = datetime.strptime(str(settings.get("square_off_time", "15:15")), "%H:%M").time()
    market_close = time(15, 30)
    sleep_seconds = int(settings.get("sleep_seconds", 60))
    cooldown_seconds = int(settings.get("cooldown_seconds", 300))
    gap_threshold_percent = float(settings["gap_threshold_percent"])
    opening_range_candles = int(settings["opening_range_candles"])
    breakout_volume_multiplier = float(settings["breakout_volume_multiplier"])
    _adaptive_stop = {
        "SIDEWAYS": float(settings.get("adaptive_stop_multiplier_sideways", 1.6)),
        "NORMAL": float(settings.get("adaptive_stop_multiplier_normal", 1.4)),
        "EXPANSION": float(settings.get("adaptive_stop_multiplier_expansion", 1.4)),
    }
    _adaptive_target = {
        "SIDEWAYS": float(settings.get("adaptive_target_multiplier_sideways", 1.1)),
        "NORMAL": float(settings.get("adaptive_target_multiplier_normal", 1.6)),
        "EXPANSION": float(settings.get("adaptive_target_multiplier_expansion", 2.2)),
    }
    _adaptive_trailing = {
        "SIDEWAYS": float(settings.get("adaptive_trailing_multiplier_sideways", 0.8)),
        "NORMAL": float(settings.get("adaptive_trailing_multiplier_normal", 1.0)),
        "EXPANSION": float(settings.get("adaptive_trailing_multiplier_expansion", 1.15)),
    }
    _adaptive_min_stop_pct = float(settings.get("adaptive_min_stop_pct", 0.0025))
    _adaptive_min_target_pct = float(settings.get("adaptive_min_target_pct", 0.0045))
    _adaptive_min_trailing_pct = float(settings.get("adaptive_min_trailing_pct", 0.002))
    _adaptive_conviction_score_weight = float(settings.get("adaptive_conviction_score_weight", 0.5))

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
        if current_time < self.market_open:
            return {
                "manage_positions": False,
                "allow_entries": False,
                "force_square_off": False,
                "allow_scan": False,
                "reason": "Waiting for market open at 09:15",
            }

        if current_time >= self.market_close:
            return {
                "manage_positions": False,
                "allow_entries": False,
                "force_square_off": False,
                "allow_scan": False,
                "reason": "Market closed for intraday trading",
            }

        if current_time >= self.square_off_time:
            return {
                "manage_positions": True,
                "allow_entries": False,
                "force_square_off": True,
                "allow_scan": False,
                "reason": "Square-off window active",
            }

        # Late-day protection: stop taking new entries well before square-off.
        cutoff_minutes = max(0, int(INTRADAY_EQUITY_ENTRY_CUTOFF_MINUTES_BEFORE_SQUAREOFF))
        entry_cutoff_time = (
            datetime.combine(now.date(), self.square_off_time) - timedelta(minutes=cutoff_minutes)
        ).time()
        if current_time >= entry_cutoff_time:
            return {
                "manage_positions": True,
                "allow_entries": False,
                "force_square_off": False,
                "allow_scan": True,
                "reason": (
                    f"Entry cutoff reached ({entry_cutoff_time.strftime('%H:%M')}) - managing only"
                ),
            }

        return {
            "manage_positions": True,
            "allow_entries": True,
            "force_square_off": False,
            "allow_scan": True,
            "reason": "Intraday session active",
        }

    def normalize_entry_signal(self, signal):
        return signal if signal in {"BUY", "SELL"} else None

    def evaluate_position_exit(self, position, latest_candle):
        return evaluate_exit(position, latest_candle, include_target=True)

    def get_signal_exit_reason(self, position, signal):
        if signal in {"BUY", "SELL"} and signal != position["side"]:
            required = max(1, int(REVERSAL_EXIT_CONFIRMATION_CANDLES))
            streak = int(position.get("reversal_streak") or 0) + 1
            position["reversal_streak"] = streak
            if streak >= required:
                position["reversal_streak"] = 0
                return "REVERSAL"
            return None

        # Reset streak when signal is aligned or HOLD/no-signal.
        if position.get("reversal_streak"):
            position["reversal_streak"] = 0
        return None

    def apply_entry_allocation_limit(
        self,
        symbol,
        quantity,
        entry_price,
        positions,
        capital,
    ):
        return quantity

    @staticmethod
    def _adaptive_regime_label(signal_score, range_distance, entry_price, atr):
        entry_price = max(float(entry_price or 0.0), 0.01)
        score = min(1.0, max(0.0, float(signal_score or 0.0)))
        range_ratio = float(range_distance or 0.0) / entry_price
        atr_ratio = float(atr or 0.0) / entry_price
        if score >= 0.035 or range_ratio >= 0.006:
            return "EXPANSION"
        if score <= 0.008 and atr_ratio <= 0.003:
            return "SIDEWAYS"
        return "NORMAL"

    def get_equity_volatility_trailing_distance(self, intraday_df):
        if intraday_df is None or intraday_df.empty:
            return 0.0
        session_df = intraday_df.loc[
            intraday_df.index.date == intraday_df.index[-1].date()
        ]
        if session_df.empty:
            return 0.0
        lookback = max(5, int(self.opening_range_candles))
        recent = session_df.tail(lookback)
        recent_ranges = recent["High"] - recent["Low"]
        avg_range = float(recent_ranges.mean()) if not recent_ranges.empty else 0.0
        if avg_range != avg_range:
            return 0.0
        return max(0.0, avg_range * float(self.breakout_volume_multiplier))

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
        base_atr = max(float(atr or 0.0), float(entry_price) * 0.0015)
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
        range_distance = max(float(volatility_distance or 0.0), 0.0)
        trailing_distance = max(atr_trailing_distance, range_distance)
        level1_distance = target_distance * 0.5
        level2_distance = target_distance
        level3_distance = target_distance * 1.6
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
            "range_volatility_distance": range_distance,
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
                float(level1_distance) * 0.8,
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
            "adaptive_regime": level_spec["adaptive_regime"],
            "adaptive_signal_score": level_spec["adaptive_signal_score"],
            "adaptive_level1_target": level_spec["level1_target"],
            "adaptive_level2_target": level_spec["level2_target"],
            "adaptive_level3_target": level_spec["level3_target"],
            "range_volatility_distance": level_spec["range_volatility_distance"],
            "atr_trailing_distance": level_spec["atr_trailing_distance"],
        }
        if extra_fields:
            payload.update(extra_fields)
        return build_position(**payload)

    def requires_extended_intraday_history(
        self,
        mode,
        strategy_name=None,
        strategies=None,
    ):
        return (
            mode == "3"
            or strategy_name == "BREAKOUT"
            or (strategies and "BREAKOUT" in strategies)
        )

    def get_vwap_bias(self, intraday_df):
        vwap = float(compute_vwap(intraday_df).iloc[-1])
        close = float(intraday_df.iloc[-1]["Close"])
        if close > vwap:
            return "BULLISH"
        if close < vwap:
            return "BEARISH"
        return "NEUTRAL"

    def passes_vwap_bias_gate(self, signal, intraday_df):
        bias = self.get_vwap_bias(intraday_df)
        if signal == "BUY":
            return bias == "BULLISH"
        if signal == "SELL":
            return bias == "BEARISH"
        return True

    def passes_breakout_volume_filter(self, intraday_history_df):
        if intraday_history_df is None or intraday_history_df.empty:
            return True, "No extended intraday history"

        latest = intraday_history_df.iloc[-1]
        latest_timestamp = intraday_history_df.index[-1]
        current_volume = float(latest["Volume"])
        same_time_mask = (
            intraday_history_df.index.strftime("%H:%M")
            == latest_timestamp.strftime("%H:%M")
        )
        prior_days_mask = (
            intraday_history_df.index.date
            != latest_timestamp.date()
        )
        reference = intraday_history_df.loc[
            same_time_mask & prior_days_mask,
            "Volume",
        ]

        if reference.empty:
            return True, "No time-matched history"

        average_volume = float(reference.mean())
        required_volume = average_volume * self.breakout_volume_multiplier
        passed = current_volume >= required_volume
        reason = (
            f"Current volume={current_volume:.0f}, "
            f"Avg matched volume={average_volume:.0f}, "
            f"Required={required_volume:.0f}"
        )
        return passed, reason

    def apply_signal_filters(
        self,
        evaluation,
        intraday_df,
        intraday_history_df=None,
        min_confirmations=1,
        analytics=None,
    ):
        del analytics
        details = {
            name: {
                "signal": item["signal"],
                "score": item["score"],
            }
            for name, item in evaluation["details"].items()
        }

        breakout_note = None
        for strategy_name, item in details.items():
            if strategy_name != "BREAKOUT":
                continue
            if item["signal"] not in {"BUY", "SELL"}:
                continue

            passed, reason = self.passes_breakout_volume_filter(
                intraday_history_df,
            )
            breakout_note = reason
            if not passed:
                item["signal"] = "HOLD"
                item["score"] = 0.0

        buy_count = sum(
            1 for item in details.values() if item["signal"] == "BUY"
        )
        sell_count = sum(
            1 for item in details.values() if item["signal"] == "SELL"
        )
        threshold = max(1, min_confirmations)
        final_signal = "HOLD"
        agreement_count = max(buy_count, sell_count)

        if buy_count >= threshold and buy_count > sell_count:
            final_signal = "BUY"
            agreement_count = buy_count
        elif sell_count >= threshold and sell_count > buy_count:
            final_signal = "SELL"
            agreement_count = sell_count

        if final_signal in {"BUY", "SELL"} and not self.passes_vwap_bias_gate(
            final_signal,
            intraday_df,
        ):
            final_signal = "HOLD"
            agreement_count = 0

        score = 0.0
        if final_signal in {"BUY", "SELL"}:
            score = sum(
                item["score"]
                for item in details.values()
                if item["signal"] == final_signal
            )

        filtered = {
            "signal": final_signal,
            "agreement_count": agreement_count,
            "score": score,
            "details": details,
            "vwap_bias": self.get_vwap_bias(intraday_df),
        }
        if breakout_note:
            filtered["breakout_volume_note"] = breakout_note
        return filtered

    def calculate_gap_percent(self, prev_close, today_open):
        if prev_close <= 0:
            return 0.0
        return ((today_open - prev_close) / prev_close) * 100

    def classify_gap(self, gap_percent):
        if gap_percent > self.gap_threshold_percent:
            return "GAP_UP"
        if gap_percent < -self.gap_threshold_percent:
            return "GAP_DOWN"
        return "NO_GAP"

    def detect_open_behavior(self, intraday_df):
        if len(intraday_df) < self.opening_range_candles:
            return "PENDING_OPEN_RANGE"

        opening_range = intraday_df.iloc[: self.opening_range_candles]
        latest = intraday_df.iloc[-1]
        price = float(latest["Close"])
        vwap = float(compute_vwap(intraday_df).iloc[-1])
        orb_high = float(opening_range["High"].max())
        orb_low = float(opening_range["Low"].min())
        avg_opening_volume = float(opening_range["Volume"].mean())
        latest_volume = float(latest["Volume"])

        if (
            price > orb_high
            and price > vwap
            and latest_volume >= avg_opening_volume
        ):
            return "GAP_GO"
        if price < orb_low and price < vwap:
            return "GAP_FILL"
        return "SIDEWAYS"

    def select_strategies(self, gap_type, behavior):
        if gap_type in {"GAP_UP", "GAP_DOWN"}:
            if behavior == "GAP_GO":
                return ["ORB", "VWAP", "BREAKOUT"], 2
            if behavior == "GAP_FILL":
                return ["ORB", "VWAP"], 2
            return ["VWAP", "RSI"], 2

        return ["MA", "RSI"], max(2, int(INTRADAY_EQUITY_AUTO_NORMAL_MIN_CONFIRMATIONS))

    def build_market_context(self, symbol, intraday_df, daily_df):
        if intraday_df.empty:
            return {
                "gap_percent": 0.0,
                "gap_type": "UNKNOWN",
                "behavior": "NO_DATA",
                "strategies": ["VWAP"],
                "min_confirmations": 1,
                "allow_entries": False,
                "cacheable": False,
                "reason": f"No intraday data for {symbol}",
            }

        if len(daily_df) < 2:
            return {
                "gap_percent": 0.0,
                "gap_type": "UNKNOWN",
                "behavior": "INSUFFICIENT_DAILY_DATA",
                "strategies": ["MA", "RSI"],
                "min_confirmations": 1,
                "allow_entries": False,
                "cacheable": False,
                "reason": f"Not enough daily history to calculate gap for {symbol}",
            }

        prev_close = float(daily_df["Close"].iloc[-2])
        today_open = float(intraday_df.iloc[0]["Open"])
        gap_percent = self.calculate_gap_percent(prev_close, today_open)
        gap_type = self.classify_gap(gap_percent)
        behavior = self.detect_open_behavior(intraday_df)

        if behavior == "PENDING_OPEN_RANGE":
            return {
                "gap_percent": gap_percent,
                "gap_type": gap_type,
                "behavior": behavior,
                "strategies": ["ORB"],
                "min_confirmations": 1,
                "allow_entries": False,
                "cacheable": False,
                "reason": (
                    f"Waiting for first {self.opening_range_candles} candles "
                    f"before adaptive entries on {symbol}"
                ),
            }

        strategies, min_confirmations = self.select_strategies(
            gap_type,
            behavior,
        )

        if gap_type != "NO_GAP" and behavior == "GAP_GO":
            strategies = [item for item in strategies if item != "RSI"]

        return {
            "gap_percent": gap_percent,
            "gap_type": gap_type,
            "behavior": behavior,
            "strategies": strategies,
            "min_confirmations": min_confirmations,
            "allow_entries": True,
            "cacheable": True,
            "reason": (
                f"{symbol} context {gap_type} / {behavior} -> "
                f"{strategies} with {min_confirmations} confirmations"
            ),
        }

    def reconcile_startup(self, execution_mode, persisted_positions):
        if execution_mode != "LIVE":
            log_event(
                f"[RECON] {self.name} running in paper mode - using persisted positions"
            )
            return persisted_positions

        # Import symbol tables and safety config
        from config import NIFTY50_SYMBOLS, MANUAL_SYMBOL_TABLE, SINGLE_SYMBOL_TABLE, ONLY_MANAGE_CONFIGURED_SYMBOLS

        broker_positions = {}
        for item in get_intraday_positions():
            quantity = int(item.get("quantity", 0))
            if quantity == 0:
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

            side = "BUY" if quantity > 0 else "SELL"
            broker_positions[symbol] = build_position(
                symbol=symbol,
                side=side,
                quantity=abs(quantity),
                entry_price=float(item.get("average_price") or 0),
                sl_pct=self.sl_percent,
                target_pct=self.target_percent,
                trailing_pct=self.trailing_percent,
            )

        if broker_positions:
            filter_status = "filtered to configured symbols only" if ONLY_MANAGE_CONFIGURED_SYMBOLS else "all positions"
            log_event(
                f"[RECON] Loaded {len(broker_positions)} live intraday positions from broker "
                f"({filter_status})"
            )
            return broker_positions

        log_event("[RECON] No live intraday positions at broker startup sync")
        return {}
