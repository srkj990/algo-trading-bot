from datetime import datetime, time

from config import (
    INTRADAY_OPTIONS_EXPIRY_WARNING_DAYS,
    INTRADAY_OPTIONS_IV_EXPANSION_MAX_IV_PERCENTILE,
    INTRADAY_OPTIONS_MAX_TRADES_PER_UNDERLYING,
    INTRADAY_OPTIONS_MAX_HOLD_MINUTES,
    INTRADAY_OPTIONS_MIN_RANGE_PCT,
    INTRADAY_OPTIONS_MIN_SIGNAL_SCORE,
    INTRADAY_OPTIONS_REGIME_EXPANSION_IV_CHANGE_PCT,
    INTRADAY_OPTIONS_REGIME_EXPANSION_RANGE_PCT,
    INTRADAY_OPTIONS_REGIME_SIDEWAYS_RANGE_PCT,
    INTRADAY_OPTIONS_REGIME_SIDEWAYS_VWAP_DEV_PCT,
    INTRADAY_OPTIONS_SIDEWAYS_LOOKBACK_CANDLES,
    INTRADAY_OPTIONS_SIDEWAYS_VWAP_BAND_PCT,
    INTRADAY_OPTIONS_TIME_EXIT_CUTOFF,
    INTRADAY_OPTIONS_VEGA_CRUSH_BLOCK_PERCENT,
)
from engines.common import build_position, merge_persisted_position_state
from executor_fno import get_options_positions
from fno_data_fetcher import get_contract_lot_size, get_fno_spot_quote_symbol
from data_fetcher import get_data
from indicators import compute_vwap
from logger import log_event
from risk_manager import calculate_target_price

from .options_equity import OptionsEquityEngine


class IntradayOptionsEngine(OptionsEquityEngine):
    name = "intraday_options"
    data_period = "10d"
    data_interval = "1m"
    order_product = "MIS"
    supported_strategies = {
        "1": "ATM_MOMENTUM",
        "2": "ATM_ORB",
        "3": "ATM_VWAP_REVERSION",
        "4": "ATM_MULTI",
        "5": "ATM_BREAKOUT_EXPANSION",
        "6": "ATM_IV_EXPANSION",
        "7": "ATM_TRAP_REVERSAL",
    }
    entry_profiles = {
        "ATM_MOMENTUM": "MOMENTUM",
        "ATM_ORB": "MOMENTUM",
        "ATM_BREAKOUT_EXPANSION": "MOMENTUM",
        "ATM_VWAP_REVERSION": "MEAN_REVERSION",
        "ATM_TRAP_REVERSAL": "MEAN_REVERSION",
        "ATM_IV_EXPANSION": "VOLATILITY",
        "ATM_MULTI": "HYBRID",
    }
    market_open = time(9, 20)
    entry_cutoff = time(15, 10)
    square_off_time = time(15, 15)
    market_close = time(15, 30)
    sleep_seconds = 15
    cooldown_seconds = 180
    require_closed_signal_candle = True
    max_symbol_allocation = 0.2
    min_contract_price = 8.0
    min_abs_delta = 0.2
    max_buy_iv_percentile = 85.0
    min_sell_iv_percentile = 15.0
    max_trades_per_underlying_per_day = INTRADAY_OPTIONS_MAX_TRADES_PER_UNDERLYING
    expiry_warning_days = INTRADAY_OPTIONS_EXPIRY_WARNING_DAYS
    vega_crush_block_percent = INTRADAY_OPTIONS_VEGA_CRUSH_BLOCK_PERCENT
    min_underlying_range_pct = INTRADAY_OPTIONS_MIN_RANGE_PCT
    min_signal_score = INTRADAY_OPTIONS_MIN_SIGNAL_SCORE
    max_hold_minutes = INTRADAY_OPTIONS_MAX_HOLD_MINUTES
    iv_expansion_max_iv_percentile = INTRADAY_OPTIONS_IV_EXPANSION_MAX_IV_PERCENTILE
    sideways_vwap_band_pct = INTRADAY_OPTIONS_SIDEWAYS_VWAP_BAND_PCT
    sideways_lookback_candles = INTRADAY_OPTIONS_SIDEWAYS_LOOKBACK_CANDLES
    momentum_volume_multiplier = 1.5
    momentum_spike_multiplier = 2.0
    momentum_min_body_ratio = 0.6
    momentum_quality_lookback = 20
    momentum_fast_ema_span = 9
    momentum_confirmation_timeout_candles = 3
    momentum_pullback_timeout_candles = 5
    momentum_pullback_band_pct = 0.0035
    mean_reversion_max_body_ratio = 0.55
    mean_reversion_spike_multiplier = 1.4
    mean_reversion_retest_band_pct = 0.0035
    mean_reversion_quality_lookback = 20
    volatility_min_body_ratio = 0.45
    volatility_range_multiplier = 1.2
    volatility_quality_lookback = 20
    volatility_regime_expansion_range_pct = INTRADAY_OPTIONS_REGIME_EXPANSION_RANGE_PCT
    volatility_regime_sideways_range_pct = INTRADAY_OPTIONS_REGIME_SIDEWAYS_RANGE_PCT
    volatility_regime_sideways_vwap_dev_pct = INTRADAY_OPTIONS_REGIME_SIDEWAYS_VWAP_DEV_PCT
    volatility_regime_expansion_iv_change_pct = INTRADAY_OPTIONS_REGIME_EXPANSION_IV_CHANGE_PCT
    time_exit_cutoff = datetime.strptime(
        INTRADAY_OPTIONS_TIME_EXIT_CUTOFF, "%H:%M"
    ).time()
    runner_level_exit_fractions = (0.3, 0.4, 0.3)

    def __init__(self, sl_percent, target_percent, trailing_percent):
        super().__init__(sl_percent, target_percent, trailing_percent)
        self.momentum_entry_setups = {}
        self.runtime_state_dirty = False

    @staticmethod
    def _runner_regime_label(analytics):
        label = str((analytics or {}).get("volatility_regime") or "NORMAL").upper()
        return label if label in {"SIDEWAYS", "NORMAL", "EXPANSION"} else "NORMAL"

    def _build_runner_lot_plan(self, quantity, lot_size):
        total_lots = max(1, int(quantity) // max(1, int(lot_size or 1)))
        if total_lots < 2:
            return [0, 0, int(quantity)]

        level1_lots = max(1, int(round(total_lots * self.runner_level_exit_fractions[0])))
        remaining_after_level1 = max(1, total_lots - level1_lots)
        level2_lots = 0
        if remaining_after_level1 >= 2:
            level2_lots = max(1, int(round(total_lots * self.runner_level_exit_fractions[1])))
            level2_lots = min(level2_lots, remaining_after_level1 - 1)
        runner_lots = max(1, total_lots - level1_lots - level2_lots)
        if (level1_lots + level2_lots + runner_lots) != total_lots:
            runner_lots = total_lots - level1_lots - level2_lots
        return [
            level1_lots * int(lot_size),
            level2_lots * int(lot_size),
            runner_lots * int(lot_size),
        ]

    def get_trend_adaptive_level_spec(
        self,
        *,
        entry_price,
        side,
        atr,
        signal_score,
        analytics,
    ):
        regime = self._runner_regime_label(analytics)
        base_atr = max(float(atr or 0.0), float(entry_price) * 0.015)
        normalized_score = min(1.0, max(0.0, float(signal_score or 0.0)))
        conviction = 1.0 + normalized_score

        stop_multiplier = {
            "SIDEWAYS": 2.0,
            "NORMAL": 1.7,
            "EXPANSION": 1.35,
        }[regime]
        target_multiplier = {
            "SIDEWAYS": 1.1,
            "NORMAL": 1.7,
            "EXPANSION": 2.3,
        }[regime]
        trailing_multiplier = {
            "SIDEWAYS": 1.0,
            "NORMAL": 0.9,
            "EXPANSION": 0.75,
        }[regime]

        stop_distance = max(float(entry_price) * 0.05, base_atr * stop_multiplier)
        target_distance = max(float(entry_price) * 0.08, base_atr * target_multiplier * conviction)
        trailing_distance = max(float(entry_price) * 0.035, base_atr * trailing_multiplier)
        level1_distance = target_distance * 0.5
        level2_distance = target_distance
        level3_distance = target_distance * 1.6
        stop_loss_price = (
            float(entry_price) - stop_distance
            if side == "BUY"
            else float(entry_price) + stop_distance
        )
        return {
            "runner_regime": regime,
            "runner_signal_score": normalized_score,
            "stop_distance": stop_distance,
            "stop_loss_price": stop_loss_price,
            "trailing_distance": trailing_distance,
            "level1_target": calculate_target_price(side, float(entry_price), level1_distance),
            "level2_target": calculate_target_price(side, float(entry_price), level2_distance),
            "level3_target": calculate_target_price(side, float(entry_price), level3_distance),
            "trailing_activation_distance": max(float(trailing_distance), float(level1_distance) * 0.8),
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
        analytics,
        lot_size,
        now,
        entry_analytics,
        engine_name,
        execution_mode,
        order_product,
        extra_fields=None,
    ):
        level_spec = self.get_trend_adaptive_level_spec(
            entry_price=entry_price,
            side=side,
            atr=atr,
            signal_score=signal_score,
            analytics=analytics,
        )
        trailing_stop = float(level_spec["stop_loss_price"])
        exit_quantities = self._build_runner_lot_plan(quantity, lot_size)

        payload = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entry_price": float(entry_price),
            "stop_loss": level_spec["stop_loss_price"],
            "target": level_spec["level3_target"],
            "trailing_stop": trailing_stop,
            "trailing_distance": level_spec["trailing_distance"],
            "trailing_activation_distance": level_spec["trailing_activation_distance"],
            "trailing_active": False,
            "atr": atr,
            "stop_distance": level_spec["stop_distance"],
            "lot_size": lot_size,
            "entry_analytics": entry_analytics,
            "entry_time": now.isoformat(),
            "engine_name": engine_name,
            "execution_mode": execution_mode,
            "order_product": order_product,
            "runner_enabled": True,
            "runner_regime": level_spec["runner_regime"],
            "runner_signal_score": level_spec["runner_signal_score"],
            "runner_level1_target": level_spec["level1_target"],
            "runner_level2_target": level_spec["level2_target"],
            "runner_level3_target": level_spec["level3_target"],
            "runner_exit_quantities": exit_quantities,
            "runner_exits_completed": [False, False, False],
            "runner_last_level_hit": None,
            "runner_stop_distance": level_spec["stop_distance"],
        }
        if extra_fields:
            payload.update(extra_fields)
        return build_position(**payload)

    def get_runner_partial_exit(self, position, snapshot, now):
        del now
        if not position.get("runner_enabled"):
            return None
        if str(position.get("pair_id") or "").strip():
            return None
        if position.get("side") != "BUY":
            return None

        latest_high = float(snapshot["latest_candle"]["High"])
        exit_quantities = list(position.get("runner_exit_quantities") or [])
        completed = list(position.get("runner_exits_completed") or [False, False, False])
        level_targets = [
            position.get("runner_level1_target"),
            position.get("runner_level2_target"),
        ]
        for index, target in enumerate(level_targets):
            if completed[index]:
                continue
            planned_qty = int(exit_quantities[index] or 0)
            if planned_qty <= 0:
                completed[index] = True
                position["runner_exits_completed"] = completed
                continue
            if latest_high >= float(target):
                return {
                    "level_index": index,
                    "reason": f"RUNNER_L{index + 1}_TARGET",
                    "quantity": min(planned_qty, int(position.get("quantity") or 0)),
                    "target_price": float(target),
                }
        return None

    def apply_runner_partial_exit(self, position, action, exit_price, snapshot):
        del snapshot
        completed = list(position.get("runner_exits_completed") or [False, False, False])
        index = int(action["level_index"])
        completed[index] = True
        position["runner_exits_completed"] = completed
        position["runner_last_level_hit"] = index + 1
        entry_price = float(position["entry_price"])
        exit_price = float(exit_price)
        if position["side"] == "BUY":
            if index == 0:
                position["stop_loss"] = max(float(position["stop_loss"]), entry_price)
                position["trailing_stop"] = max(float(position["trailing_stop"]), entry_price)
            elif index == 1:
                protected_level = max(
                    float(position["runner_level1_target"]),
                    exit_price - max(float(position.get("trailing_distance") or 0.0), 0.01),
                )
                position["stop_loss"] = max(float(position["stop_loss"]), protected_level)
                position["trailing_stop"] = max(float(position["trailing_stop"]), protected_level)
                position["target"] = float(position["runner_level3_target"])
                position["trailing_active"] = True

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
                "reason": "Waiting for options market open at 09:15",
            }

        if current_time >= self.market_close:
            return {
                "manage_positions": False,
                "allow_entries": False,
                "force_square_off": False,
                "allow_scan": False,
                "reason": "Market closed for intraday options trading",
            }

        if current_time >= self.square_off_time:
            return {
                "manage_positions": True,
                "allow_entries": False,
                "force_square_off": True,
                "allow_scan": False,
                "reason": "Intraday options square-off window active",
            }

        if current_time >= self.entry_cutoff:
            return {
                "manage_positions": True,
                "allow_entries": False,
                "force_square_off": False,
                "allow_scan": True,
                "reason": "Intraday options entry cutoff reached",
            }

        return {
            "manage_positions": True,
            "allow_entries": True,
            "force_square_off": False,
            "allow_scan": True,
            "reason": "Intraday options session active",
        }

    def apply_entry_allocation_limit(
        self,
        symbol,
        quantity,
        entry_price,
        positions,
        capital,
    ):
        capped = super().apply_entry_allocation_limit(
            symbol,
            quantity,
            entry_price,
            positions,
            capital,
        )
        lot_size = get_contract_lot_size(symbol)
        return (capped // lot_size) * lot_size

    def apply_signal_filters(
        self,
        evaluation,
        intraday_df,
        intraday_history_df=None,
        min_confirmations=1,
        analytics=None,
    ):
        del intraday_history_df, min_confirmations
        filtered = dict(evaluation)
        if analytics is None:
            filtered["options_filter_note"] = "Greeks unavailable"
            return filtered

        filtered["analytics"] = analytics
        notes = []

        session_df = intraday_df.loc[
            intraday_df.index.date == intraday_df.index[-1].date()
        ]
        latest_close = float(session_df.iloc[-1]["Close"])
        option_vwap = float(compute_vwap(session_df).iloc[-1])
        filtered["vwap_bias"] = (
            "BULLISH" if latest_close > option_vwap
            else "BEARISH" if latest_close < option_vwap
            else "NEUTRAL"
        )

        session_open = float(session_df.iloc[0]["Open"])
        session_high = float(session_df["High"].max())
        session_low = float(session_df["Low"].min())
        range_pct = (
            ((session_high - session_low) / session_open) * 100.0
            if session_open > 0
            else 0.0
        )
        filtered["range_pct"] = range_pct
        regime = self.build_volatility_regime_context(session_df, analytics)
        filtered["volatility_regime"] = regime["label"]
        filtered["selected_profile"] = evaluation.get("selected_profile")
        filtered["components"] = evaluation.get("components")
        analytics["volatility_regime"] = regime["label"]
        analytics["volatility_regime_context"] = regime

        if range_pct < self.min_underlying_range_pct:
            filtered["signal"] = "HOLD"
            filtered["agreement_count"] = 0
            filtered["score"] = 0.0
            filtered["options_filter_note"] = (
                f"Volatility proxy blocked trade: range {range_pct:.2f}% "
                f"below minimum {self.min_underlying_range_pct:.2f}%"
            )
            return filtered

        # Strong sideways blocker: if recent price stays close to VWAP with a muted ATR ratio,
        # we do not want to bleed premium in chop.
        recent_window = session_df.tail(max(3, int(self.sideways_lookback_candles)))
        recent_vwap = compute_vwap(recent_window)
        recent_deviation = (
            (recent_window["Close"] - recent_vwap).abs() / recent_vwap.replace(0, 1)
        ).fillna(0.0)
        recent_range_pct = (
            ((float(recent_window["High"].max()) - float(recent_window["Low"].min())) / max(session_open, 1.0)) * 100.0
            if not recent_window.empty
            else 0.0
        )
        if (
            not recent_window.empty
            and recent_deviation.max() <= float(self.sideways_vwap_band_pct)
            and recent_range_pct <= self.min_underlying_range_pct
        ):
            filtered["signal"] = "HOLD"
            filtered["agreement_count"] = 0
            filtered["score"] = 0.0
            filtered["options_filter_note"] = (
                f"Sideways blocker: recent VWAP deviation stayed within "
                f"{self.sideways_vwap_band_pct:.4f} and range was only {recent_range_pct:.2f}%"
            )
            return filtered

        if analytics and not analytics.get("skip_underlying_bias"):
            bias = self.get_underlying_bias(analytics["underlying"])
            filtered["underlying_bias"] = bias["bias"]
            analytics["underlying_bias"] = bias["bias"]
            notes.append(
                f"Underlying bias {bias['bias']} | EMA={bias['ema']:.2f} "
                f"| VWAP={bias['vwap']:.2f} | Spot={bias['close']:.2f}"
            )
            option_type = (analytics.get("option_type") or "").upper()
            if option_type == "CE" and bias["bias"] != "BULLISH":
                filtered["signal"] = "HOLD"
                filtered["agreement_count"] = 0
                filtered["score"] = 0.0
                filtered["options_filter_note"] = (
                    f"Underlying bias filter blocked CE: {bias['bias']}"
                )
                return filtered
            if option_type == "PE" and bias["bias"] != "BEARISH":
                filtered["signal"] = "HOLD"
                filtered["agreement_count"] = 0
                filtered["score"] = 0.0
                filtered["options_filter_note"] = (
                    f"Underlying bias filter blocked PE: {bias['bias']}"
                )
                return filtered

        if filtered["signal"] == "BUY" and latest_close <= option_vwap:
            filtered["signal"] = "HOLD"
            filtered["agreement_count"] = 0
            filtered["score"] = 0.0
            filtered["options_filter_note"] = (
                f"VWAP band filter blocked BUY: price {latest_close:.2f} "
                f"not above VWAP {option_vwap:.2f}"
            )
            return filtered

        if filtered["signal"] == "SELL" and latest_close >= option_vwap:
            filtered["signal"] = "HOLD"
            filtered["agreement_count"] = 0
            filtered["score"] = 0.0
            filtered["options_filter_note"] = (
                f"VWAP band filter blocked SELL: price {latest_close:.2f} "
                f"not below VWAP {option_vwap:.2f}"
            )
            return filtered

        if analytics["option_price"] < self.min_contract_price:
            filtered["signal"] = "HOLD"
            filtered["agreement_count"] = 0
            filtered["score"] = 0.0
            filtered["options_filter_note"] = (
                f"Premium {analytics['option_price']:.2f} below minimum "
                f"{self.min_contract_price:.2f}"
            )
            return filtered

        iv_change_15m_pct = analytics.get("iv_change_15m_pct")
        if (
            iv_change_15m_pct is not None
            and iv_change_15m_pct <= -abs(self.vega_crush_block_percent)
        ):
            filtered["signal"] = "HOLD"
            filtered["agreement_count"] = 0
            filtered["score"] = 0.0
            filtered["options_filter_note"] = (
                f"Vega crush alert: IV changed {iv_change_15m_pct:.1f}% "
                f"in last 15 minutes"
            )
            return filtered

        abs_delta = abs(float(analytics["delta"]))
        if filtered["signal"] == "BUY" and abs_delta < self.min_abs_delta:
            filtered["signal"] = "HOLD"
            filtered["agreement_count"] = 0
            filtered["score"] = 0.0
            filtered["options_filter_note"] = (
                f"Delta {analytics['delta']:.3f} below minimum absolute delta "
                f"{self.min_abs_delta:.2f}"
            )
            return filtered

        iv_percentile = analytics.get("iv_percentile")
        if (
            filtered.get("strategy") == "ATM_IV_EXPANSION"
            and iv_percentile is not None
            and iv_percentile > self.iv_expansion_max_iv_percentile
        ):
            filtered["signal"] = "HOLD"
            filtered["agreement_count"] = 0
            filtered["score"] = 0.0
            filtered["options_filter_note"] = (
                f"IV expansion setup requires low IV percentile, got {iv_percentile:.1f} "
                f"above threshold {self.iv_expansion_max_iv_percentile:.1f}"
            )
            return filtered
        if filtered["signal"] == "BUY" and iv_percentile is not None:
            if iv_percentile > self.max_buy_iv_percentile:
                filtered["signal"] = "HOLD"
                filtered["agreement_count"] = 0
                filtered["score"] = 0.0
                filtered["options_filter_note"] = (
                    f"IV percentile {iv_percentile:.1f} above buy ceiling "
                    f"{self.max_buy_iv_percentile:.1f}"
                )
                return filtered

        if filtered["signal"] == "SELL" and iv_percentile is not None:
            if iv_percentile < self.min_sell_iv_percentile:
                filtered["signal"] = "HOLD"
                filtered["agreement_count"] = 0
                filtered["score"] = 0.0
                filtered["options_filter_note"] = (
                    f"IV percentile {iv_percentile:.1f} below sell floor "
                    f"{self.min_sell_iv_percentile:.1f}"
                )
                return filtered

        if analytics.get("days_to_expiry", 99) < self.expiry_warning_days:
            notes.append(
                f"Expiry warning: {analytics['days_to_expiry']} day(s) left"
            )

        entry_profile = self.resolve_entry_profile(filtered, analytics=analytics)
        if filtered["signal"] in {"BUY", "SELL"}:
            profile_reason = None
            passed = True
            if entry_profile == "MOMENTUM":
                passed, profile_reason = self.validate_momentum_entry(
                    filtered["signal"],
                    intraday_df,
                    analytics,
                    latest_close=latest_close,
                    option_vwap=option_vwap,
                    strategy_name=filtered.get("strategy"),
                )
            elif entry_profile == "MEAN_REVERSION":
                passed, profile_reason = self.validate_mean_reversion_entry(
                    filtered["signal"],
                    intraday_df,
                    analytics,
                    latest_close=latest_close,
                    option_vwap=option_vwap,
                )
            elif entry_profile == "VOLATILITY":
                passed, profile_reason = self.validate_volatility_entry(
                    filtered["signal"],
                    intraday_df,
                    analytics,
                    latest_close=latest_close,
                    option_vwap=option_vwap,
                )

            if not passed:
                filtered["signal"] = "HOLD"
                filtered["agreement_count"] = 0
                filtered["score"] = 0.0
                filtered["options_filter_note"] = profile_reason
                return filtered
            if profile_reason:
                notes.append(profile_reason)

        if regime["label"] != "UNKNOWN":
            notes.append(
                f"Volatility regime {regime['label']} | SessionRange={regime['session_range_pct']:.2f}% "
                f"| RecentRange={regime['recent_range_pct']:.2f}% | VWAPDev={regime['recent_vwap_deviation']:.4f}"
            )

        filtered["score"] += abs_delta * 0.2
        if iv_percentile is not None:
            filtered["score"] += (iv_percentile / 100.0) * 0.05
        if notes:
            filtered["options_filter_note"] = " | ".join(notes)
        if filtered["signal"] in {"BUY", "SELL"} and filtered["score"] < self.min_signal_score:
            original_score = filtered["score"]
            filtered["signal"] = "HOLD"
            filtered["agreement_count"] = 0
            filtered["score"] = 0.0
            filtered["options_filter_note"] = (
                f"Signal score {original_score:.4f} below minimum "
                f"{self.min_signal_score:.4f}"
            )
        return filtered

    def get_entry_profile(self, strategy_name):
        return self.entry_profiles.get(strategy_name)

    def resolve_entry_profile(self, evaluation, analytics=None):
        strategy_name = (evaluation or {}).get("strategy")
        if strategy_name == "ATM_MULTI":
            details = (evaluation or {}).get("details") or {}
            selected_profile = (
                (evaluation or {}).get("selected_profile")
                or details.get("ATM_MULTI", {}).get("selected_profile")
            )
            if selected_profile in {"MOMENTUM", "MEAN_REVERSION", "VOLATILITY"}:
                return selected_profile
        return self.get_entry_profile(strategy_name)

    def build_volatility_regime_context(self, intraday_df, analytics=None):
        if intraday_df is None or intraday_df.empty:
            return {
                "label": "UNKNOWN",
                "session_range_pct": 0.0,
                "recent_range_pct": 0.0,
                "recent_vwap_deviation": 0.0,
                "iv_change_15m_pct": None,
            }

        session_df = intraday_df.loc[
            intraday_df.index.date == intraday_df.index[-1].date()
        ]
        session_open = float(session_df.iloc[0]["Open"]) if not session_df.empty else 0.0
        session_high = float(session_df["High"].max()) if not session_df.empty else 0.0
        session_low = float(session_df["Low"].min()) if not session_df.empty else 0.0
        session_range_pct = (
            ((session_high - session_low) / session_open) * 100.0
            if session_open > 0
            else 0.0
        )

        recent_window = session_df.tail(max(3, int(self.sideways_lookback_candles)))
        if recent_window.empty:
            recent_range_pct = 0.0
            recent_vwap_deviation = 0.0
        else:
            recent_vwap = compute_vwap(recent_window)
            recent_vwap_deviation = float(
                (
                    (recent_window["Close"] - recent_vwap).abs()
                    / recent_vwap.replace(0, 1)
                ).fillna(0.0).max()
            )
            recent_range_pct = (
                (
                    (
                        float(recent_window["High"].max())
                        - float(recent_window["Low"].min())
                    )
                    / max(session_open, 1.0)
                )
                * 100.0
            )

        iv_change_15m_pct = (analytics or {}).get("iv_change_15m_pct")
        label = "NORMAL"
        if (
            session_range_pct >= float(self.volatility_regime_expansion_range_pct)
            or (
                iv_change_15m_pct is not None
                and iv_change_15m_pct
                >= float(self.volatility_regime_expansion_iv_change_pct)
            )
        ):
            label = "EXPANSION"
        elif (
            session_range_pct <= float(self.volatility_regime_sideways_range_pct)
            and recent_vwap_deviation
            <= float(self.volatility_regime_sideways_vwap_dev_pct)
        ):
            label = "SIDEWAYS"

        return {
            "label": label,
            "session_range_pct": session_range_pct,
            "recent_range_pct": recent_range_pct,
            "recent_vwap_deviation": recent_vwap_deviation,
            "iv_change_15m_pct": iv_change_15m_pct,
        }

    def hydrate_runtime_state(self, saved_state):
        runtime_state = dict(saved_state.get("engine_runtime_state") or {})
        setups = runtime_state.get("momentum_entry_setups") or {}
        self.momentum_entry_setups = {
            str(key): value
            for key, value in setups.items()
            if isinstance(value, dict)
        }
        self.runtime_state_dirty = False

    def export_runtime_state(self):
        return {
            "momentum_entry_setups": dict(self.momentum_entry_setups),
        }

    def _mark_runtime_state_dirty(self):
        self.runtime_state_dirty = True

    def _clear_momentum_setup(self, setup_key):
        if setup_key in self.momentum_entry_setups:
            self.momentum_entry_setups.pop(setup_key, None)
            self._mark_runtime_state_dirty()

    def _store_momentum_setup(self, setup_key, payload):
        self.momentum_entry_setups[setup_key] = payload
        self._mark_runtime_state_dirty()

    def _get_momentum_setup_key(self, strategy_name, analytics, signal):
        underlying = str((analytics or {}).get("underlying") or "UNKNOWN")
        option_type = str((analytics or {}).get("option_type") or signal or "UNKNOWN")
        strategy = str(strategy_name or "UNKNOWN")
        return f"{underlying}:{strategy}:{option_type.upper()}:{signal}"

    def _build_momentum_snapshot(
        self,
        signal,
        intraday_df,
        analytics,
        latest_close=None,
        option_vwap=None,
    ):
        if intraday_df is None or intraday_df.empty:
            return None, "Momentum entry validator blocked trade: option candles unavailable"

        session_df = intraday_df.loc[
            intraday_df.index.date == intraday_df.index[-1].date()
        ]
        lookback = max(5, int(self.momentum_quality_lookback))
        if len(session_df) < lookback:
            return None, (
                "Momentum entry validator blocked trade: "
                f"need at least {lookback} session candles"
            )
        if len(session_df) < 2:
            return None, "Momentum entry validator blocked trade: need at least 2 candles"

        latest = session_df.iloc[-1]
        previous = session_df.iloc[-2]
        latest_close = float(latest_close if latest_close is not None else latest["Close"])
        option_vwap = float(
            option_vwap if option_vwap is not None else compute_vwap(session_df).iloc[-1]
        )
        latest_open = float(latest["Open"])
        latest_high = float(latest["High"])
        latest_low = float(latest["Low"])
        latest_volume = float(latest["Volume"])
        candle_range = max(latest_high - latest_low, 0.0)
        body = abs(latest_close - latest_open)
        body_ratio = body / max(candle_range, 1e-9)

        recent = session_df.tail(lookback)
        avg_volume = float(recent["Volume"].mean())
        recent_ranges = recent["High"] - recent["Low"]
        avg_range = float(recent_ranges.mean())
        ema_fast = float(
            session_df["Close"]
            .ewm(span=int(self.momentum_fast_ema_span), adjust=False)
            .mean()
            .iloc[-1]
        )

        prev_high = float(previous["High"])
        prev_low = float(previous["Low"])
        volume_spike = latest_volume >= (avg_volume * float(self.momentum_volume_multiplier))
        no_spike = candle_range <= (avg_range * float(self.momentum_spike_multiplier))
        strong_candle = body_ratio >= float(self.momentum_min_body_ratio)
        bias = str((analytics or {}).get("underlying_bias") or "")
        trend_aligned = (
            (signal == "BUY" and bias == "BULLISH" and latest_close > option_vwap)
            or (signal == "SELL" and bias == "BEARISH" and latest_close < option_vwap)
        )
        breakout_detected = (
            (signal == "BUY" and latest_close > prev_high)
            or (signal == "SELL" and latest_close < prev_low)
        )
        pullback_band = max(option_vwap, ema_fast) * float(self.momentum_pullback_band_pct)
        pullback_ready = (
            signal == "BUY"
            and latest_low <= max(option_vwap, ema_fast) + pullback_band
            and latest_close >= ema_fast
            and latest_close > option_vwap
        ) or (
            signal == "SELL"
            and latest_high >= min(option_vwap, ema_fast) - pullback_band
            and latest_close <= ema_fast
            and latest_close < option_vwap
        )

        return {
            "session_df": session_df,
            "trade_day": session_df.index[-1].date().isoformat(),
            "candle_count": len(session_df),
            "latest_close": latest_close,
            "latest_high": latest_high,
            "latest_low": latest_low,
            "latest_volume": latest_volume,
            "candle_range": candle_range,
            "body_ratio": body_ratio,
            "avg_volume": avg_volume,
            "avg_range": avg_range,
            "ema_fast": ema_fast,
            "option_vwap": option_vwap,
            "prev_high": prev_high,
            "prev_low": prev_low,
            "volume_spike": volume_spike,
            "no_spike": no_spike,
            "strong_candle": strong_candle,
            "bias": bias,
            "trend_aligned": trend_aligned,
            "breakout_detected": breakout_detected,
            "pullback_ready": pullback_ready,
        }, None

    def validate_momentum_entry(
        self,
        signal,
        intraday_df,
        analytics,
        latest_close=None,
        option_vwap=None,
        strategy_name=None,
    ):
        snapshot, error = self._build_momentum_snapshot(
            signal,
            intraday_df,
            analytics,
            latest_close=latest_close,
            option_vwap=option_vwap,
        )
        if snapshot is None:
            return False, error
        volatility_regime = str((analytics or {}).get("volatility_regime") or "UNKNOWN")

        setup_key = self._get_momentum_setup_key(strategy_name, analytics, signal)
        setup = self.momentum_entry_setups.get(setup_key)
        if setup and setup.get("trade_day") != snapshot["trade_day"]:
            self._clear_momentum_setup(setup_key)
            setup = None

        if not snapshot["trend_aligned"]:
            self._clear_momentum_setup(setup_key)
            return False, (
                "Momentum entry validator blocked trade: "
                f"trend alignment failed ({snapshot['bias'] or 'UNKNOWN'})"
            )
        if volatility_regime == "SIDEWAYS":
            self._clear_momentum_setup(setup_key)
            return False, (
                "Momentum entry validator blocked trade: "
                "volatility regime is SIDEWAYS, so breakout confirmation is not trusted"
            )

        if setup and setup.get("state") == "awaiting_pullback":
            waited = snapshot["candle_count"] - int(
                setup.get("confirmed_candle_count", snapshot["candle_count"])
            )
            if snapshot["pullback_ready"]:
                self._clear_momentum_setup(setup_key)
                return True, (
                    "Momentum pullback entry ready: "
                    f"body_ratio={snapshot['body_ratio']:.2f}, volume={snapshot['latest_volume']:.0f}, "
                    f"ema_fast={snapshot['ema_fast']:.2f}, vwap={snapshot['option_vwap']:.2f}, "
                    f"bias={snapshot['bias']}"
                )
            if waited >= int(self.momentum_pullback_timeout_candles):
                self._clear_momentum_setup(setup_key)
                return False, (
                    "Momentum entry validator blocked trade: pullback window "
                    f"timed out after {waited} candle(s)"
                )
            return False, (
                "Momentum setup confirmed: waiting for pullback near "
                f"EMA{self.momentum_fast_ema_span}/VWAP"
            )

        if not snapshot["strong_candle"]:
            self._clear_momentum_setup(setup_key)
            return False, (
                "Momentum entry validator blocked trade: "
                f"body ratio {snapshot['body_ratio']:.2f} below threshold {self.momentum_min_body_ratio:.2f}"
            )
        if not snapshot["volume_spike"]:
            self._clear_momentum_setup(setup_key)
            return False, (
                "Momentum entry validator blocked trade: "
                f"volume {snapshot['latest_volume']:.0f} below required spike "
                f"{snapshot['avg_volume'] * self.momentum_volume_multiplier:.0f}"
            )
        if not snapshot["no_spike"]:
            self._clear_momentum_setup(setup_key)
            return False, (
                "Momentum entry validator blocked trade: "
                f"range {snapshot['candle_range']:.2f} exceeded spike limit "
                f"{snapshot['avg_range'] * self.momentum_spike_multiplier:.2f}"
            )

        if setup and setup.get("state") == "awaiting_confirmation":
            breakout_level = float(setup.get("breakout_level", 0.0))
            confirmation_passed = (
                signal == "BUY" and snapshot["latest_close"] > breakout_level
            ) or (
                signal == "SELL" and snapshot["latest_close"] < breakout_level
            )
            if confirmation_passed:
                self._store_momentum_setup(
                    setup_key,
                    {
                        "state": "awaiting_pullback",
                        "trade_day": snapshot["trade_day"],
                        "confirmed_candle_count": snapshot["candle_count"],
                        "breakout_level": breakout_level,
                    },
                )
                return False, (
                    "Momentum setup confirmed: waiting for pullback near "
                    f"EMA{self.momentum_fast_ema_span}/VWAP after breakout level {breakout_level:.2f}"
                )

            waited = snapshot["candle_count"] - int(setup.get("armed_candle_count", snapshot["candle_count"]))
            if waited >= int(self.momentum_confirmation_timeout_candles):
                self._clear_momentum_setup(setup_key)
                return False, (
                    "Momentum entry validator blocked trade: follow-through confirmation "
                    f"timed out after {waited} candle(s)"
                )
            return False, (
                "Momentum setup armed: waiting for follow-through "
                f"{'above' if signal == 'BUY' else 'below'} {breakout_level:.2f}"
            )

        if not snapshot["breakout_detected"]:
            self._clear_momentum_setup(setup_key)
            return False, (
                "Momentum entry validator blocked trade: breakout candle "
                f"did not clear the previous {'high' if signal == 'BUY' else 'low'}"
            )

        breakout_level = snapshot["latest_high"] if signal == "BUY" else snapshot["latest_low"]
        self._store_momentum_setup(
            setup_key,
            {
                "state": "awaiting_confirmation",
                "trade_day": snapshot["trade_day"],
                "armed_candle_count": snapshot["candle_count"],
                "breakout_level": breakout_level,
            },
        )
        return False, (
            "Momentum setup armed: waiting for follow-through "
            f"{'above' if signal == 'BUY' else 'below'} {breakout_level:.2f}"
        )

    def validate_mean_reversion_entry(
        self,
        signal,
        intraday_df,
        analytics,
        latest_close=None,
        option_vwap=None,
    ):
        if intraday_df is None or intraday_df.empty:
            return False, "Mean-reversion entry validator blocked trade: option candles unavailable"

        session_df = intraday_df.loc[
            intraday_df.index.date == intraday_df.index[-1].date()
        ]
        lookback = max(5, int(self.mean_reversion_quality_lookback))
        if len(session_df) < lookback:
            return False, (
                "Mean-reversion entry validator blocked trade: "
                f"need at least {lookback} session candles"
            )

        latest = session_df.iloc[-1]
        latest_close = float(latest_close if latest_close is not None else latest["Close"])
        option_vwap = float(option_vwap if option_vwap is not None else compute_vwap(session_df).iloc[-1])
        latest_open = float(latest["Open"])
        latest_high = float(latest["High"])
        latest_low = float(latest["Low"])
        candle_range = max(latest_high - latest_low, 0.0)
        body = abs(latest_close - latest_open)
        body_ratio = body / max(candle_range, 1e-9)

        recent = session_df.tail(lookback)
        recent_ranges = recent["High"] - recent["Low"]
        avg_range = float(recent_ranges.mean())
        near_vwap_band = option_vwap * float(self.mean_reversion_retest_band_pct)
        near_vwap = (
            latest_low <= option_vwap + near_vwap_band
            if signal == "BUY"
            else latest_high >= option_vwap - near_vwap_band
        )
        controlled_candle = body_ratio <= float(self.mean_reversion_max_body_ratio)
        no_momentum_spike = candle_range <= (
            avg_range * float(self.mean_reversion_spike_multiplier)
        )
        volatility_regime = str((analytics or {}).get("volatility_regime") or "UNKNOWN")

        if volatility_regime == "EXPANSION":
            return False, (
                "Mean-reversion entry validator blocked trade: "
                "volatility regime is EXPANSION, so fading price is disabled"
            )

        if not near_vwap:
            return False, (
                "Mean-reversion entry validator blocked trade: "
                f"price did not retest VWAP zone around {option_vwap:.2f}"
            )
        if not controlled_candle:
            return False, (
                "Mean-reversion entry validator blocked trade: "
                f"body ratio {body_ratio:.2f} above threshold {self.mean_reversion_max_body_ratio:.2f}"
            )
        if not no_momentum_spike:
            return False, (
                "Mean-reversion entry validator blocked trade: "
                f"range {candle_range:.2f} exceeded controlled-candle limit "
                f"{avg_range * self.mean_reversion_spike_multiplier:.2f}"
            )

        return True, (
            "Mean-reversion entry validator passed: "
            f"body_ratio={body_ratio:.2f}, range={candle_range:.2f}, "
            f"avg_range={avg_range:.2f}, vwap={option_vwap:.2f}"
        )

    def validate_volatility_entry(
        self,
        signal,
        intraday_df,
        analytics,
        latest_close=None,
        option_vwap=None,
    ):
        if intraday_df is None or intraday_df.empty:
            return False, "Volatility entry validator blocked trade: option candles unavailable"

        session_df = intraday_df.loc[
            intraday_df.index.date == intraday_df.index[-1].date()
        ]
        lookback = max(5, int(self.volatility_quality_lookback))
        if len(session_df) < lookback:
            return False, (
                "Volatility entry validator blocked trade: "
                f"need at least {lookback} session candles"
            )

        latest = session_df.iloc[-1]
        latest_close = float(latest_close if latest_close is not None else latest["Close"])
        option_vwap = float(option_vwap if option_vwap is not None else compute_vwap(session_df).iloc[-1])
        latest_open = float(latest["Open"])
        latest_high = float(latest["High"])
        latest_low = float(latest["Low"])
        candle_range = max(latest_high - latest_low, 0.0)
        body = abs(latest_close - latest_open)
        body_ratio = body / max(candle_range, 1e-9)

        recent = session_df.tail(lookback)
        recent_ranges = recent["High"] - recent["Low"]
        avg_range = float(recent_ranges.mean())
        iv_percentile = analytics.get("iv_percentile")
        iv_change_15m_pct = analytics.get("iv_change_15m_pct")
        bias = str((analytics or {}).get("underlying_bias") or "")
        trend_aligned = (
            (signal == "BUY" and bias == "BULLISH" and latest_close > option_vwap)
            or (signal == "SELL" and bias == "BEARISH" and latest_close < option_vwap)
        )
        expansion_range = candle_range >= (
            avg_range * float(self.volatility_range_multiplier)
        )
        strong_enough = body_ratio >= float(self.volatility_min_body_ratio)
        iv_supportive = iv_percentile is not None and (
            iv_change_15m_pct is None or iv_change_15m_pct >= 0.0
        )
        volatility_regime = str((analytics or {}).get("volatility_regime") or "UNKNOWN")

        if not trend_aligned:
            return False, f"Volatility entry validator blocked trade: trend alignment failed ({bias or 'UNKNOWN'})"
        if volatility_regime not in {"EXPANSION", "NORMAL"}:
            return False, (
                "Volatility entry validator blocked trade: "
                f"volatility regime {volatility_regime} is not supportive"
            )
        if not iv_supportive:
            return False, (
                "Volatility entry validator blocked trade: "
                "IV percentile unavailable or short-term IV change is not supportive"
            )
        if not expansion_range:
            return False, (
                "Volatility entry validator blocked trade: "
                f"range {candle_range:.2f} below expansion threshold "
                f"{avg_range * self.volatility_range_multiplier:.2f}"
            )
        if not strong_enough:
            return False, (
                "Volatility entry validator blocked trade: "
                f"body ratio {body_ratio:.2f} below threshold {self.volatility_min_body_ratio:.2f}"
            )

        return True, (
            "Volatility entry validator passed: "
            f"body_ratio={body_ratio:.2f}, range={candle_range:.2f}, "
            f"avg_range={avg_range:.2f}, iv_percentile={iv_percentile:.1f}, bias={bias}, regime={volatility_regime}"
        )

    def get_underlying_bias(self, underlying):
        underlying_df = get_data(
            get_fno_spot_quote_symbol(underlying),
            period="2d",
            interval="1m",
            provider="KITE",
        )
        if underlying_df.empty:
            raise RuntimeError(f"No underlying data for {underlying}")

        session_df = underlying_df.loc[
            underlying_df.index.date == underlying_df.index[-1].date()
        ]
        close = float(session_df.iloc[-1]["Close"])
        vwap = float(compute_vwap(session_df).iloc[-1])
        ema = float(session_df["Close"].ewm(span=21, adjust=False).mean().iloc[-1])
        if close > vwap and close > ema:
            bias = "BULLISH"
        elif close < vwap and close < ema:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"
        return {
            "bias": bias,
            "close": close,
            "vwap": vwap,
            "ema": ema,
        }

    def get_time_exit_reason(self, position, now):
        entry_time_raw = position.get("entry_time")
        if self.max_hold_minutes > 0 and entry_time_raw:
            try:
                entry_time = datetime.fromisoformat(entry_time_raw)
                held_minutes = (now - entry_time).total_seconds() / 60.0
                if held_minutes >= self.max_hold_minutes:
                    return f"TIME_EXIT_{self.max_hold_minutes}M"
            except ValueError:
                pass

        if now.time() >= self.time_exit_cutoff:
            return f"TIME_EXIT_{self.time_exit_cutoff.strftime('%H:%M')}"

        return None

    def get_trade_frequency_key(self, symbol, analytics=None):
        del symbol
        if analytics and analytics.get("underlying"):
            return analytics["underlying"]
        return None

    def get_max_trades_per_day(self):
        return self.max_trades_per_underlying_per_day

    def reconcile_startup(self, execution_mode, persisted_positions):
        if execution_mode != "LIVE":
            log_event(
                f"[RECON] {self.name} running in paper mode - using persisted positions"
            )
            return persisted_positions

        try:
            broker_positions = {}
            for item in get_options_positions(product="MIS"):
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
                f"[RECON] Loaded {len(broker_positions)} live intraday options positions from broker"
            )
            return broker_positions
        except NotImplementedError as ex:
            log_event(f"[RECON] Intraday options startup sync unavailable: {ex}", "warning")
            return persisted_positions
