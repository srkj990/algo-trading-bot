from datetime import datetime, time

from config import (
    INTRADAY_OPTIONS_EXPIRY_WARNING_DAYS,
    INTRADAY_OPTIONS_MAX_TRADES_PER_UNDERLYING,
    INTRADAY_OPTIONS_MAX_HOLD_MINUTES,
    INTRADAY_OPTIONS_MIN_RANGE_PCT,
    INTRADAY_OPTIONS_MIN_SIGNAL_SCORE,
    INTRADAY_OPTIONS_TIME_EXIT_CUTOFF,
    INTRADAY_OPTIONS_VEGA_CRUSH_BLOCK_PERCENT,
)
from engines.common import build_position, merge_persisted_position_state
from executor_fno import get_options_positions
from fno_data_fetcher import get_contract_lot_size, get_fno_spot_quote_symbol
from data_fetcher import get_data
from indicators import compute_vwap
from logger import log_event

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
    time_exit_cutoff = datetime.strptime(
        INTRADAY_OPTIONS_TIME_EXIT_CUTOFF, "%H:%M"
    ).time()

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

        if range_pct < self.min_underlying_range_pct:
            filtered["signal"] = "HOLD"
            filtered["agreement_count"] = 0
            filtered["score"] = 0.0
            filtered["options_filter_note"] = (
                f"Volatility proxy blocked trade: range {range_pct:.2f}% "
                f"below minimum {self.min_underlying_range_pct:.2f}%"
            )
            return filtered

        if analytics and not analytics.get("skip_underlying_bias"):
            bias = self.get_underlying_bias(analytics["underlying"])
            filtered["underlying_bias"] = bias["bias"]
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
