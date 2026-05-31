from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import re

import pandas as pd
import yfinance as yf

from data_fetcher import get_data
from config import (
    BACKTEST_DEFAULTS,
    FNO_INDEX_SYMBOLS,
    FNO_UNDERLYING_DETAILS,
    INTRADAY_EQUITY_AUTO_NORMAL_MIN_CONFIRMATIONS,
    MANUAL_SYMBOL_TABLE,
    NIFTY50_SYMBOLS,
    SINGLE_SYMBOL_TABLE,
    get_runtime_config,
    get_risk_style_presets,
    TRANSACTION_COST_MODEL_ENABLED,
    TRANSACTION_SLIPPAGE_PCT_PER_SIDE,
)
from engines import (
    DeliveryEquityEngine,
    FuturesEquityEngine,
    IntradayEquityEngine,
    IntradayFuturesEngine,
    IntradayOptionsEngine,
    OptionsEquityEngine,
)
from engines.common import build_position, evaluate_exit, resolve_trade_targets, update_trailing_stop
from fno_data_fetcher import (
    get_available_expiries,
    get_available_option_strikes,
    get_contract_lot_size,
    get_fno_display_name,
    get_fno_spot_quote_symbol,
    get_option_greeks_snapshot,
    get_options_data,
    resolve_option_contract,
)
from models.position_adapter import (
    calculate_position_pnl,
    position_entry_price,
    position_quantity,
    position_side,
    signed_position_value,
)
from orchestration.positions import normalize_partial_exit_quantity
from orchestration.signal_workflow import scan_symbols, should_enter_trade
from risk_manager import atr_position_size, position_size, resolve_daily_max_loss_pct
from signal_scoring import get_atr_value
from transaction_costs import estimate_intraday_equity_round_trip_cost
from transaction_costs import (
    estimate_delivery_equity_round_trip_cost,
    estimate_futures_round_trip_cost,
    estimate_options_round_trip_cost,
)

ENGINE_OPTIONS = {
    "1": IntradayEquityEngine,
    "2": DeliveryEquityEngine,
    "3": FuturesEquityEngine,
    "4": OptionsEquityEngine,
    "5": IntradayFuturesEngine,
    "6": IntradayOptionsEngine,
}

SUPPORTED_BACKTEST_FNO_SYMBOLS = {
    "NIFTY": "^NSEI",
    "SENSEX": "^BSESN",
}
BACKTEST_FNO_SYMBOLS = {
    key: SUPPORTED_BACKTEST_FNO_SYMBOLS[key]
    for key in FNO_INDEX_SYMBOLS
    if key in SUPPORTED_BACKTEST_FNO_SYMBOLS
}
BACKTEST_DEFAULT_DATA = {
    engine_name: (
        str(values["period"]),
        str(values["interval"]),
    )
    for engine_name, values in BACKTEST_DEFAULTS["default_data"].items()
}
BACKTEST_PROMPT_DEFAULTS = BACKTEST_DEFAULTS["prompt_defaults"]
ENGINE_FALLBACK_LEVELS = {
    "sl_percent": 0.5,
    "target_percent": 1.0,
    "trailing_percent": 0.35,
}
RESULTS_DIR = Path("Results") / "BackTest"
VALID_BACKTEST_PERIODS = (
    "1d",
    "5d",
    "1mo",
    "3mo",
    "6mo",
    "1y",
    "2y",
    "5y",
)
VALID_BACKTEST_INTERVALS = (
    "1m",
    "2m",
    "5m",
    "15m",
    "30m",
    "60m",
    "90m",
    "1d",
)

@dataclass
class BacktestConfig:
    engine_name: str
    capital: float
    period: str
    interval: str
    strategy_mode: str
    strategy_name: str | None
    strategies: tuple[str, ...]
    min_confirmations: int
    risk_percent: float
    atr_stop_multiplier: float
    trailing_atr_multiplier: float
    target_risk_reward: float
    risk_style_name: str
    top_n: int
    max_positions: int
    max_capital_per_trade: float
    max_capital_deployed: float
    universe: tuple[str, ...]
    one_trade_per_symbol_per_day: bool = True
    intraday_options_lot_mode: str = "CAPITAL_BASED"
    intraday_options_entry_mode: str = "LIVE_STAGED"
    summary_lines: list[str] = field(default_factory=list)
    option_backtest_settings: dict[str, Any] | None = None
    # Tick-entry simulation (opt-in).
    # When True, entry fills are simulated at an estimated intra-candle price
    # (rather than candle close) to model early breakout entry via ticks.
    tick_entry_enabled: bool = True


def _prompt_default(key, fallback):
    return BACKTEST_PROMPT_DEFAULTS.get(key, fallback)


def _prompt_default_int(key, fallback):
    return int(_prompt_default(key, fallback))


def _prompt_default_float(key, fallback):
    return float(_prompt_default(key, fallback))


def _supported_backtest_fno_choices():
    return [
        {
            "index": index,
            "symbol": symbol,
            "display_name": str(FNO_UNDERLYING_DETAILS[symbol]["display_name"]),
        }
        for index, symbol in enumerate(BACKTEST_FNO_SYMBOLS, start=1)
    ]


class BacktestEngine:
    def __init__(self, config: BacktestConfig, log_fn=None, cancel_event=None, trade_fn=None):
        self.config = config
        self._log = log_fn or (lambda *a, **k: None)
        self._cancel = cancel_event
        self._on_trade = trade_fn or (lambda t: None)
        self.cash = config.capital
        self.positions = {}
        self.trades = []
        self.equity_curve = []
        self.traded_symbols_by_day = defaultdict(set)
        self.trade_counts_by_day = defaultdict(dict)
        self.regime_cache = {}
        self.session_runtime_state = {}
        self.engine_helper = self._build_engine_helper()

    def _build_engine_helper(self):
        sl_percent = float(ENGINE_FALLBACK_LEVELS["sl_percent"])
        target_percent = float(ENGINE_FALLBACK_LEVELS["target_percent"])
        trailing_percent = float(ENGINE_FALLBACK_LEVELS["trailing_percent"])
        if self.config.engine_name == "intraday_equity":
            return IntradayEquityEngine(sl_percent, target_percent, trailing_percent)
        if self.config.engine_name == "delivery_equity":
            return DeliveryEquityEngine(sl_percent, target_percent, trailing_percent)
        if self.config.engine_name == "futures_equity":
            return FuturesEquityEngine(sl_percent, target_percent, trailing_percent)
        if self.config.engine_name == "options_equity":
            return OptionsEquityEngine(sl_percent, target_percent, trailing_percent)
        if self.config.engine_name == "intraday_futures":
            return IntradayFuturesEngine(sl_percent, target_percent, trailing_percent)
        if self.config.engine_name == "intraday_options":
            engine = IntradayOptionsEngine(sl_percent, target_percent, trailing_percent)
            engine.momentum_entry_mode = (
                "LEGACY_RAW"
                if str(self.config.intraday_options_entry_mode).upper() == "LEGACY_IMMEDIATE"
                else "STAGED"
            )
            return engine
        return None

    def fetch_history(self):
        if self.config.engine_name == "intraday_options":
            return self._fetch_intraday_options_history()

        history = {}
        for symbol in self.config.universe:
            data = yf.download(
                symbol,
                period=self.config.period,
                interval=self.config.interval,
                auto_adjust=False,
                progress=False,
            )
            if hasattr(data.columns, "levels"):
                data.columns = [col[0] for col in data.columns]
            if not data.empty:
                history[symbol] = self._normalize_history_frame(data)
        if self.config.engine_name == "delivery_equity":
            index_data = yf.download(
                "^NSEI",
                period=self.config.period,
                interval="1d",
                auto_adjust=False,
                progress=False,
            )
            if hasattr(index_data.columns, "levels"):
                index_data.columns = [col[0] for col in index_data.columns]
            if not index_data.empty:
                history[self.engine_helper.nifty_trend_symbol] = self._normalize_history_frame(
                    index_data
                )
        return history

    @staticmethod
    def _normalize_history_frame(dataframe):
        normalized = dataframe.copy()
        normalized.index = pd.to_datetime(normalized.index)
        if getattr(normalized.index, "tz", None) is not None:
            normalized.index = (
                normalized.index.tz_convert("Asia/Kolkata").tz_localize(None)
            )
        return normalized.sort_index()

    def _fetch_intraday_options_history(self):
        settings = dict(self.config.option_backtest_settings or {})
        if settings.get("structure_mode") != "SINGLE":
            raise RuntimeError(
                "Intraday options premium backtest currently supports only ATM SINGLE OPTION structure."
            )

        base_symbol = str(settings["base_symbol"])
        expiry = str(settings["expiry"])
        underlying_symbol = str(
            settings.get("underlying_symbol") or get_fno_spot_quote_symbol(base_symbol)
        )
        underlying_data = get_data(
            underlying_symbol,
            period=self.config.period,
            interval=self.config.interval,
            provider="KITE",
            use_cache=True,
        )
        if hasattr(underlying_data.columns, "levels"):
            underlying_data.columns = [col[0] for col in underlying_data.columns]
        if underlying_data.empty:
            raise RuntimeError(
                f"No underlying historical data fetched for {base_symbol} ({underlying_symbol})."
            )

        underlying_data = self._normalize_history_frame(underlying_data)
        strikes = get_available_option_strikes(base_symbol, expiry)
        if not strikes:
            raise RuntimeError(
                f"No option strikes available for {get_fno_display_name(base_symbol)} expiry {expiry}."
            )

        strike_step = self._infer_strike_step(strikes)
        offset_steps = {
            "ATM": 0,
            "ATM_PLUS_1": 1,
            "ATM_MINUS_1": -1,
        }.get(str(settings.get("strike_mode") or "ATM").upper(), 0)
        selected_strikes = self._select_dynamic_atm_strikes(
            strikes,
            underlying_data,
            offset_steps,
        )
        if not selected_strikes:
            selected_strikes = [min(strikes, key=lambda strike: abs(float(strike) - float(underlying_data["Close"].iloc[-1])))]

        history = {underlying_symbol: underlying_data}
        contracts: dict[tuple[int, str], str] = {}
        for strike in selected_strikes:
            for option_type in ("CE", "PE"):
                try:
                    contract_symbol = resolve_option_contract(
                        base_symbol,
                        expiry,
                        strike,
                        option_type,
                    )
                except Exception:
                    continue
                option_data = get_options_data(
                    contract_symbol,
                    period=self.config.period,
                    interval=self.config.interval,
                    provider="KITE",
                )
                if hasattr(option_data.columns, "levels"):
                    option_data.columns = [col[0] for col in option_data.columns]
                if option_data.empty:
                    continue
                history[contract_symbol] = self._normalize_history_frame(option_data)
                contracts[(int(strike), option_type)] = contract_symbol

        if not contracts:
            raise RuntimeError(
                f"No option premium history fetched for {get_fno_display_name(base_symbol)} expiry {expiry}."
            )

        settings["underlying_symbol"] = underlying_symbol
        settings["available_strikes"] = [int(strike) for strike in strikes]
        settings["contracts"] = contracts
        settings["strike_step"] = strike_step
        settings["signal_symbols"] = (underlying_symbol,)
        self.config.option_backtest_settings = settings
        return history

    @staticmethod
    def _infer_strike_step(strikes):
        ordered = sorted({int(float(strike)) for strike in strikes})
        if len(ordered) < 2:
            return 50
        step_candidates = [
            ordered[index + 1] - ordered[index]
            for index in range(len(ordered) - 1)
            if (ordered[index + 1] - ordered[index]) > 0
        ]
        return min(step_candidates) if step_candidates else 50

    @staticmethod
    def _select_dynamic_atm_strikes(strikes, underlying_data, offset_steps):
        ordered_strikes = sorted({int(float(strike)) for strike in strikes})
        if not ordered_strikes or underlying_data.empty:
            return []

        selected = set()
        closes = (
            underlying_data["Close"]
            .dropna()
            .astype(float)
            .tolist()
        )
        for close_price in closes:
            atm_strike = min(
                ordered_strikes,
                key=lambda strike: abs(float(strike) - float(close_price)),
            )
            atm_index = ordered_strikes.index(atm_strike)
            target_index = max(0, min(len(ordered_strikes) - 1, atm_index + int(offset_steps)))
            selected.add(int(ordered_strikes[target_index]))

        return sorted(selected)

    def run(self):
        history = self.fetch_history()
        if not history:
            raise RuntimeError("No historical data fetched for the selected universe.")

        timeline = sorted({index for df in history.values() for index in df.index})
        total = len(timeline)
        step = max(1, total // 50)

        for i, timestamp in enumerate(timeline):
            if self._cancel and self._cancel.is_set():
                raise RuntimeError("Cancelled by user")
            self._process_timestamp(history, timestamp)
            if i % step == 0 or i == total - 1:
                closed = len(self.trades)
                wins = sum(1 for t in self.trades if t.get("pnl", 0) > 0)
                running_pnl = sum(t.get("pnl", 0) for t in self.trades)
                win_rate = wins * 100 // closed if closed else 0
                self._log(
                    f"[BACKTEST] {i+1}/{total} ({(i+1)*100//total}%) | "
                    f"Trades={closed} | WinRate={win_rate}% | "
                    f"P&L=₹{running_pnl:,.0f}"
                )

        self._close_all_open_positions(history, timeline[-1])
        return self._build_summary()

    def _process_timestamp(self, history, timestamp):
        latest_prices = {}
        cycle_state = self.engine_helper.get_cycle_state(
            pd.Timestamp(timestamp).to_pydatetime(),
        )

        for symbol, df in history.items():
            current_slice = df.loc[:timestamp]
            if current_slice.empty:
                continue

            latest_candle = current_slice.iloc[-1]
            latest_prices[symbol] = float(latest_candle["Close"])

            if current_slice.index[-1] != timestamp:
                continue

            if cycle_state.get("force_square_off"):
                continue

            if symbol in self.positions:
                if self.config.engine_name == "intraday_options":
                    self._manage_intraday_options_position(
                        symbol=symbol,
                        latest_candle=latest_candle,
                        latest_close=latest_prices[symbol],
                        timestamp=timestamp,
                    )
                else:
                    position = self.positions[symbol]
                    trailing_distance = float(position.get("trailing_distance") or 0.0)
                    # Ratchet with candle High/Low first so fast intra-candle moves lock in profit
                    if position_side(position) == "BUY":
                        update_trailing_stop(position, float(latest_candle.get("High", latest_prices[symbol])), trailing_distance)
                    else:
                        update_trailing_stop(position, float(latest_candle.get("Low", latest_prices[symbol])), trailing_distance)
                    exit_reason = self.engine_helper.evaluate_position_exit(position, latest_candle)
                    if exit_reason:
                        exit_fill_price = self._resolve_exit_fill_price(
                            position=position,
                            latest_candle=latest_candle,
                            exit_reason=str(exit_reason),
                            fallback_close=latest_prices[symbol],
                        )
                        self._exit_position(symbol, exit_fill_price, timestamp, exit_reason)
                continue

        if cycle_state.get("force_square_off"):
            self._square_off_open_positions(history, timestamp, latest_prices)
            self._mark_equity(timestamp, latest_prices)
            return

        if not cycle_state.get("allow_scan", True):
            self._mark_equity(timestamp, latest_prices)
            return

        scan_result = scan_symbols(
            self._build_signal_context(history, timestamp),
            pd.Timestamp(timestamp).to_pydatetime(),
        )
        if cycle_state.get("allow_entries", True):
            self._enter_ranked_candidates(scan_result.ranked_candidates, timestamp, history)
        self._mark_equity(timestamp, latest_prices)

    def _can_enter_symbol(self, symbol, timestamp):
        if len(self.positions) >= self.config.max_positions:
            return False

        current_equity = self._current_equity({})
        deployed_capital = current_equity - self.cash
        if deployed_capital >= self.config.max_capital_deployed:
            return False

        if not self.config.one_trade_per_symbol_per_day:
            return True

        trade_day = pd.Timestamp(timestamp).date()
        return symbol not in self.traded_symbols_by_day[trade_day]

    def _signal_symbols(self):
        settings = self.config.option_backtest_settings or {}
        if self.config.engine_name == "intraday_options":
            return tuple(settings.get("signal_symbols") or self.config.universe)
        return self.config.universe

    def _build_signal_context(self, history, timestamp):
        trade_day = pd.Timestamp(timestamp).date()
        selected_symbols = list(self._signal_symbols())
        runtime_option_settings = self._build_runtime_option_settings()
        mode = "3" if self.config.strategy_mode == "AUTO_ADAPTIVE" else (
            "1" if self.config.strategy_mode == "SINGLE" else "2"
        )
        selected_history = history

        def fetch_data(symbol, period=None, interval=None, provider=None, use_cache=False):
            del provider, use_cache
            frame = selected_history.get(symbol)
            if frame is None:
                return pd.DataFrame()
            slice_df = frame.loc[:timestamp]
            if slice_df.empty:
                return slice_df
            requested_interval = str(interval or self.config.interval)
            if requested_interval == self.config.interval:
                return slice_df.copy()
            if requested_interval == "1d":
                return self._build_daily_history(slice_df)
            return slice_df.copy()

        logger = SimpleNamespace(exception=lambda *args, **kwargs: None)
        return SimpleNamespace(
            config=SimpleNamespace(
                selected_symbols=selected_symbols,
                data_provider="BACKTEST",
                mode=mode,
                strategy_name=self.config.strategy_name,
                strategies=list(self.config.strategies),
                min_confirmations=self.config.min_confirmations,
                atm_option_config=runtime_option_settings,
                option_pair_config=None,
            ),
            engine=self.engine_helper,
            positions=self.positions,
            regime_cache=self.regime_cache,
            fetch_data=fetch_data,
            log_event=self._log,
            logger=logger,
            traded_symbols_today=self.traded_symbols_by_day[trade_day],
            trade_counts_today=self.trade_counts_by_day[trade_day],
            active_trade_day=trade_day,
            last_entry_time=0.0,
            session_runtime_state=self.session_runtime_state,
            save_engine_state=lambda **kwargs: None,
        )

    def _build_runtime_option_settings(self):
        settings = self.config.option_backtest_settings or {}
        if self.config.engine_name != "intraday_options" or settings.get("structure_mode") != "SINGLE":
            return None
        strike_mode = str(settings.get("strike_mode") or "ATM").upper()
        strike_offset = {"ATM": 0, "ATM_PLUS_1": 1, "ATM_MINUS_1": -1}.get(strike_mode, 0)
        return {
            "scan_symbol": str(settings.get("underlying_symbol") or self.config.universe[0]),
            "underlying": str(settings.get("base_symbol") or ""),
            "expiry": str(settings.get("expiry") or ""),
            "strike_offset": strike_offset,
            "strike_offset_mode": strike_mode,
            "historical_atm_from_underlying": True,
        }

    def _build_intraday_options_candidate(
        self,
        *,
        signal_symbol,
        signal_slice,
        timestamp,
        evaluation,
        history,
    ):
        if evaluation.get("signal") not in {"BUY", "SELL"}:
            return None
        option_signal = str(evaluation.get("option_signal") or "")
        option_type = str(evaluation.get("option_type") or "")
        if option_signal not in {"BUY_CE", "BUY_PE"}:
            return None
        if option_signal.endswith("CE"):
            option_type = "CE"
        elif option_signal.endswith("PE"):
            option_type = "PE"

        settings = self.config.option_backtest_settings or {}
        strikes = [int(strike) for strike in settings.get("available_strikes") or []]
        contracts = settings.get("contracts") or {}
        if not strikes or not contracts:
            return None

        underlying_close = float(signal_slice.iloc[-1]["Close"])
        atm_strike = min(strikes, key=lambda strike: abs(float(strike) - underlying_close))
        atm_index = strikes.index(atm_strike)
        strike_mode = str(settings.get("strike_mode") or "ATM").upper()
        offset_steps = {"ATM": 0, "ATM_PLUS_1": 1, "ATM_MINUS_1": -1}.get(strike_mode, 0)
        target_index = max(0, min(len(strikes) - 1, atm_index + offset_steps))
        strike = int(strikes[target_index])
        contract_symbol = contracts.get((strike, option_type))
        if not contract_symbol:
            return None

        option_history = history.get(contract_symbol)
        if option_history is None:
            return None
        option_slice = option_history.loc[:timestamp]
        if option_slice.empty or option_slice.index[-1] != timestamp:
            return None

        latest_close = float(option_slice.iloc[-1]["Close"])
        atr_value = get_atr_value(option_slice)
        if latest_close <= 0 or atr_value <= 0:
            return None

        option_history_daily = self._build_daily_history(option_history)
        try:
            option_analytics = get_option_greeks_snapshot(
                contract_symbol,
                option_intraday=option_slice,
                underlying_intraday=signal_slice,
                option_history=option_history_daily,
                option_price=latest_close,
                underlying_price=underlying_close,
            )
        except Exception:
            option_analytics = None

        filtered_evaluation = dict(evaluation)
        if hasattr(self.engine_helper, "apply_signal_filters"):
            filtered_evaluation = self.engine_helper.apply_signal_filters(
                filtered_evaluation,
                option_slice,
                intraday_history_df=None,
                min_confirmations=self.config.min_confirmations,
                analytics=option_analytics,
                prefetched_underlying_df=signal_slice,
            )
        if filtered_evaluation.get("signal") not in {"BUY", "SELL"}:
            return None

        return {
            "symbol": contract_symbol,
            "signal": "BUY",
            "agreement_count": filtered_evaluation["agreement_count"],
            "score": filtered_evaluation["score"],
            "latest_close": latest_close,
            "atr": atr_value,
            "strategy": filtered_evaluation.get("strategy"),
            "option_signal": option_signal,
            "reason": filtered_evaluation.get("reason"),
            "trade_identity": str(settings.get("base_symbol") or signal_symbol),
            "underlying_symbol": signal_symbol,
            "strike": strike,
            "option_type": option_type,
            "analytics": option_analytics,
            "premium_volatility_distance": (
                self.engine_helper.get_premium_volatility_trailing_distance(
                    option_slice,
                    option_analytics,
                )
                if hasattr(self.engine_helper, "get_premium_volatility_trailing_distance")
                else 0.0
            ),
        }

    def _evaluate_signal(self, symbol, current_slice):
        if self.config.strategy_mode == "AUTO_ADAPTIVE":
            daily_df = self._build_daily_history(current_slice)
            context = self.engine_helper.build_market_context(symbol, current_slice, daily_df)
            if not context.get("allow_entries", False):
                return {
                    "signal": "HOLD",
                    "agreement_count": 0,
                    "score": 0.0,
                    "reason": context.get("reason"),
                    "strategy": None,
                    "option_signal": None,
                }

            evaluation = evaluate_symbol_signal(
                current_slice,
                mode="2",
                strategies=context["strategies"],
                min_confirmations=context["min_confirmations"],
            )
            filtered = self.engine_helper.apply_signal_filters(
                evaluation,
                current_slice,
                intraday_history_df=None,
                min_confirmations=context["min_confirmations"],
                analytics=None,
            )
            filtered["reason"] = context.get("reason")
            return filtered

        if self.config.strategy_mode == "SINGLE":
            evaluation = evaluate_symbol_signal(
                current_slice,
                mode="1",
                strategy_name=self.config.strategy_name,
            )
        else:
            evaluation = evaluate_symbol_signal(
                current_slice,
                mode="2",
                strategies=list(self.config.strategies),
                min_confirmations=self.config.min_confirmations,
            )

        if hasattr(self.engine_helper, "apply_signal_filters"):
            evaluation = self.engine_helper.apply_signal_filters(
                evaluation,
                current_slice,
                intraday_history_df=None,
                min_confirmations=self.config.min_confirmations,
                analytics=None,
            )
        return evaluation

    def _build_daily_history(self, intraday_slice):
        daily = intraday_slice.resample("1D").agg(
            {
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }
        )
        return daily.dropna()

    def _consecutive_losses_today(self, trade_day) -> int:
        day_str = trade_day.isoformat()
        closed_today = [
            t for t in self.trades
            if t.get("exit_time") and str(t["exit_time"])[:10] == day_str
        ]
        closed_today.sort(key=lambda t: str(t["exit_time"]), reverse=True)
        count = 0
        for t in closed_today:
            if float(t.get("net_pnl", t.get("pnl", 0)) or 0) < 0:
                count += 1
            else:
                break
        return count

    def _enter_ranked_candidates(self, ranked_candidates, timestamp, history=None):
        trade_day = pd.Timestamp(timestamp).date()
        risk_cfg = get_runtime_config().risk_controls

        # Consecutive loss guard — mirrors session.py risk check
        consec_limit = int(risk_cfg.consecutive_loss_limit or 0)
        if consec_limit > 0 and self._consecutive_losses_today(trade_day) >= consec_limit:
            self._log(
                f"[RISK] Consecutive loss limit reached ({consec_limit}) — entries blocked",
                "warning",
            )
            return

        # Daily max loss guard — mirrors session.py risk check
        daily_max_loss_pct = resolve_daily_max_loss_pct(
            float(self.config.capital), float(risk_cfg.daily_max_loss_pct or 0)
        )
        if daily_max_loss_pct > 0:
            day_str = trade_day.isoformat()
            day_pnl = sum(
                float(t.get("net_pnl", t.get("pnl", 0)) or 0)
                for t in self.trades
                if t.get("exit_time") and str(t["exit_time"])[:10] == day_str
            )
            starting_equity = float(self.config.capital)
            if day_pnl < -(starting_equity * daily_max_loss_pct):
                self._log(
                    f"[RISK] Daily max loss exceeded ({daily_max_loss_pct*100:.1f}%) — entries blocked",
                    "warning",
                )
                return

        for candidate in ranked_candidates[: self.config.top_n]:
            if len(self.positions) >= self.config.max_positions:
                break
            if candidate["symbol"] in self.positions:
                continue
            if candidate["atr"] <= 0:
                continue

            trade_day = pd.Timestamp(timestamp).date()
            trade_identity = str(candidate.get("trade_identity") or candidate["symbol"])
            if (
                self.config.one_trade_per_symbol_per_day
                and trade_identity in self.traded_symbols_by_day[trade_day]
            ):
                continue

            current_equity = self._current_equity({})
            deployed_capital = current_equity - self.cash
            remaining_deployable = max(0.0, self.config.max_capital_deployed - deployed_capital)
            if remaining_deployable <= 0:
                break

            if self.config.engine_name == "intraday_options":
                min_contract_price = float(
                    getattr(self.engine_helper, "min_contract_price", 0.0) or 0.0
                )
                if float(candidate["latest_close"]) < min_contract_price:
                    continue
                lot_size = (
                    get_contract_lot_size(candidate["symbol"])
                    if ":" in candidate["symbol"]
                    else 1
                )
                if self.config.intraday_options_lot_mode == "ONE_LOT":
                    qty = int(lot_size)
                else:
                    available_capital = min(
                        float(self.cash),
                        float(self.config.max_capital_per_trade),
                        float(remaining_deployable),
                    )
                    qty = int(available_capital / candidate["latest_close"])
            elif (
                self.config.engine_name in {"intraday_equity", "delivery_equity"}
                and hasattr(self.engine_helper, "get_trend_adaptive_level_spec")
            ):
                level_spec = self.engine_helper.get_trend_adaptive_level_spec(
                    entry_price=float(candidate["latest_close"]),
                    side=candidate["signal"],
                    atr=float(candidate["atr"] or 0.0),
                    signal_score=float(candidate.get("score") or 0.0),
                    volatility_distance=float(candidate.get("equity_volatility_distance") or 0.0),
                )
                qty = position_size(
                    current_equity,
                    float(candidate["latest_close"]),
                    float(level_spec["stop_loss_price"]),
                    self.config.risk_percent,
                )
            else:
                sizing = atr_position_size(
                    capital=current_equity,
                    entry_price=candidate["latest_close"],
                    atr_value=candidate["atr"],
                    atr_multiplier=self.config.atr_stop_multiplier,
                    risk_percent=self.config.risk_percent,
                )
                qty = sizing["quantity"]
            qty = min(qty, int(self.config.max_capital_per_trade / candidate["latest_close"]))
            qty = min(qty, int(remaining_deployable / candidate["latest_close"]))
            if self.config.engine_name == "intraday_options":
                qty = self.engine_helper.apply_entry_allocation_limit(
                    candidate["symbol"],
                    qty,
                    candidate["latest_close"],
                    self.positions,
                    current_equity,
                )
            if qty <= 0:
                continue

            if self.config.engine_name == "delivery_equity" and candidate["signal"] == "BUY":
                nifty_history = self._build_signal_context(history or {}, timestamp).fetch_data(
                    self.engine_helper.nifty_trend_symbol,
                    interval="1d",
                )
                trend_ok, trend_reason = self.engine_helper.passes_nifty_trend_guard(
                    nifty_history,
                    timestamp,
                )
                if not trend_ok:
                    self.config.summary_lines.append(
                        f"Skipped {candidate['symbol']} on {pd.Timestamp(timestamp).date()}: {trend_reason}"
                    )
                    continue

            if not should_enter_trade(
                candidate,
                self._build_entry_context(),
                entry_price=float(candidate["latest_close"]),
                quantity=int(qty),
            ):
                continue

            entry_price = candidate["latest_close"]

            # ── Tick-entry fill simulation ─────────────────────────────────
            # When tick_entry_enabled=True, model "we entered earlier in the
            # candle at the breakout level" rather than at the candle close.
            # Not applied to delivery_equity (daily candles).
            # For intraday_options, uses the option premium candle's OHLC so
            # the simulated fill captures the intra-candle ORB entry price
            # rather than the candle close (which overstates the entry cost).
            if (
                self.config.tick_entry_enabled
                and self.config.engine_name not in {"delivery_equity"}
            ):
                try:
                    if self.config.engine_name == "intraday_options":
                        src_symbol = candidate["symbol"]
                    else:
                        src_symbol = candidate.get("underlying_symbol") or candidate["symbol"]
                    candle_df = (history or {}).get(src_symbol)
                    if candle_df is not None:
                        slice_at = candle_df.loc[:timestamp]
                        if not slice_at.empty:
                            candle_row = slice_at.iloc[-1]
                            from tick_entry.manager import backtest_tick_fill
                            tick_fill = backtest_tick_fill(
                                candidate,
                                candle_open=float(candle_row.get("Open", entry_price)),
                                candle_high=float(candle_row.get("High", entry_price)),
                                candle_low=float(candle_row.get("Low", entry_price)),
                            )
                            if tick_fill is not None:
                                entry_price = tick_fill
                except Exception:
                    pass  # Fall back to candle close on any error

            actual_targets = resolve_trade_targets(
                engine_name=self.config.engine_name,
                entry_price=entry_price,
                quantity=qty,
                risk_style_name=self.config.risk_style_name,
                signal_strength=float(candidate.get("score") or 0.5),
                side=candidate["signal"],
                atr=float(candidate["atr"]),
                trailing_atr_multiplier=self.config.trailing_atr_multiplier,
                engine_helper=self.engine_helper,
            )
            trailing_distance = float(actual_targets["trailing_distance"])
            stop_distance = float(actual_targets["stop_distance"])
            trailing_activation_distance = float(actual_targets["trailing_activation_distance"])
            if self.config.engine_name == "intraday_options":
                try:
                    self.positions[candidate["symbol"]] = self.engine_helper.build_trend_adaptive_position(
                        symbol=candidate["symbol"],
                        side=candidate["signal"],
                        quantity=qty,
                        entry_price=entry_price,
                        atr=float(candidate["atr"] or 0.0),
                        signal_score=float(candidate.get("score") or 0.0),
                        analytics=dict(candidate.get("analytics") or {}),
                        lot_size=get_contract_lot_size(candidate["symbol"]) if ":" in candidate["symbol"] else 1,
                        now=pd.Timestamp(timestamp).to_pydatetime(),
                        entry_analytics=dict(candidate.get("analytics") or {}),
                        engine_name=self.config.engine_name,
                        execution_mode="PAPER",
                        order_product=self.engine_helper.order_product,
                        premium_volatility_distance=float(
                            candidate.get("premium_volatility_distance") or 0.0
                        ),
                        extra_fields={
                            "trade_identity": trade_identity,
                            "asset_class": actual_targets["asset_class"],
                            "risk_profile": actual_targets["risk_profile"],
                            "cost_to_profit_ratio": actual_targets["cost_to_profit_ratio"],
                            "expected_costs": actual_targets["expected_costs"],
                            "expected_net_profit": actual_targets["expected_net_profit"],
                            "partial_exit_events": [],
                            "realized_pnl_parts": 0.0,
                            "realized_charges_parts": 0.0,
                        },
                        risk_style_name=self.config.risk_style_name,
                    )
                except ValueError:
                    continue
                self.positions[candidate["symbol"]]["min_breakeven_price"] = float(actual_targets["min_breakeven_price"])
            elif (
                self.config.engine_name in {"intraday_equity", "delivery_equity"}
                and hasattr(self.engine_helper, "build_trend_adaptive_position")
            ):
                self.positions[candidate["symbol"]] = self.engine_helper.build_trend_adaptive_position(
                    symbol=candidate["symbol"],
                    side=candidate["signal"],
                    quantity=qty,
                    entry_price=entry_price,
                    atr=float(candidate["atr"] or 0.0),
                    signal_score=float(candidate.get("score") or 0.0),
                    now=pd.Timestamp(timestamp).to_pydatetime(),
                    engine_name=self.config.engine_name,
                    execution_mode="PAPER",
                    order_product=getattr(self.engine_helper, "order_product", "MIS"),
                    volatility_distance=float(candidate.get("equity_volatility_distance") or 0.0),
                    extra_fields={
                        "trade_identity": trade_identity,
                        "asset_class": actual_targets["asset_class"],
                        "risk_profile": actual_targets["risk_profile"],
                        "min_breakeven_price": actual_targets["min_breakeven_price"],
                        "expected_costs": actual_targets["expected_costs"],
                        "expected_net_profit": actual_targets["expected_net_profit"],
                        "cost_to_profit_ratio": actual_targets["cost_to_profit_ratio"],
                    },
                )
            else:
                self.positions[candidate["symbol"]] = build_position(
                    symbol=candidate["symbol"],
                    side=candidate["signal"],
                    quantity=qty,
                    entry_price=entry_price,
                    stop_loss=actual_targets["stop_loss"],
                    target=actual_targets["target"],
                    trailing_stop=actual_targets["trailing_stop"],
                    trailing_distance=trailing_distance,
                    trailing_activation_distance=trailing_activation_distance,
                    trailing_active=False,
                    atr=candidate["atr"],
                    stop_distance=stop_distance,
                    entry_time=pd.Timestamp(timestamp).isoformat(),
                    engine_name=self.config.engine_name,
                    execution_mode="PAPER",
                    order_product=getattr(self.engine_helper, "order_product", "CNC"),
                    trade_identity=trade_identity,
                    asset_class=actual_targets["asset_class"],
                    risk_profile=actual_targets["risk_profile"],
                    min_breakeven_price=actual_targets["min_breakeven_price"],
                    expected_costs=actual_targets["expected_costs"],
                    expected_net_profit=actual_targets["expected_net_profit"],
                    cost_to_profit_ratio=actual_targets["cost_to_profit_ratio"],
                )

            if candidate["signal"] == "BUY":
                self.cash -= entry_price * qty
            else:
                self.cash += entry_price * qty

            self.traded_symbols_by_day[trade_day].add(trade_identity)
            self.trades.append(
                {
                    "symbol": candidate["symbol"],
                    "side": candidate["signal"],
                    "entry_time": timestamp,
                    "entry_price": entry_price,
                    "candle_close_price": float(candidate["latest_close"]),
                    "tick_entry": self.config.tick_entry_enabled and entry_price != float(candidate["latest_close"]),
                    "quantity": qty,
                    "score": candidate["score"],
                    "atr": candidate["atr"],
                    "strategy": candidate.get("strategy"),
                    "option_signal": candidate.get("option_signal"),
                    "trade_identity": trade_identity,
                    "underlying_symbol": candidate.get("underlying_symbol"),
                    "strike": candidate.get("strike"),
                    "option_type": candidate.get("option_type"),
                    "entry_reason": candidate.get("reason"),
                }
            )

    def _build_entry_context(self):
        return SimpleNamespace(
            engine=self.engine_helper,
            config=SimpleNamespace(
                risk_style_name=self.config.risk_style_name,
                trailing_atr_multiplier=self.config.trailing_atr_multiplier,
            ),
            runtime_config=get_runtime_config(),
            log_event=self._log,
        )

    def _manage_intraday_options_position(self, *, symbol, latest_candle, latest_close, timestamp):
        position = self.positions.get(symbol)
        if not position:
            return

        snapshot = {
            "latest_close": float(latest_close),
            "latest_candle": {
                "High": float(latest_candle["High"]),
                "Low": float(latest_candle["Low"]),
            },
        }

        if hasattr(self.engine_helper, "get_runner_partial_exit"):
            for _ in range(3):
                action = self.engine_helper.get_runner_partial_exit(
                    position,
                    snapshot,
                    pd.Timestamp(timestamp).to_pydatetime(),
                )
                if not action:
                    break
                self._partial_exit_position(
                    symbol=symbol,
                    exit_price=float(latest_close),
                    timestamp=timestamp,
                    action=action,
                    snapshot=snapshot,
                )
                if symbol not in self.positions:
                    return

        # Ratchet trailing stop with candle High/Low before evaluating exit.
        # This simulates intra-candle price moving favourably (spike-and-crash scenario):
        # a BUY position that spikes to candle High will have its trailing stop locked in
        # at that high-water mark, so if the candle then crashes, the ratcheted stop fires.
        trailing_distance = float(position.get("trailing_distance") or 0.0)
        if position_side(position) == "BUY":
            update_trailing_stop(position, float(latest_candle.get("High", latest_close)), trailing_distance)
        else:
            update_trailing_stop(position, float(latest_candle.get("Low", latest_close)), trailing_distance)

        exit_reason = self.engine_helper.evaluate_position_exit(position, latest_candle)
        if not exit_reason and hasattr(self.engine_helper, "get_time_exit_reason"):
            exit_reason = self.engine_helper.get_time_exit_reason(
                position,
                pd.Timestamp(timestamp).to_pydatetime(),
            )
        if exit_reason:
            exit_fill_price = self._resolve_exit_fill_price(
                position=position,
                latest_candle=latest_candle,
                exit_reason=str(exit_reason),
                fallback_close=float(latest_close),
            )
            self._exit_position(symbol, exit_fill_price, timestamp, str(exit_reason))
            return

    def _partial_exit_position(self, *, symbol, exit_price, timestamp, action, snapshot):
        position = self.positions.get(symbol)
        if not position:
            return

        exit_qty = normalize_partial_exit_quantity(position, action["quantity"])
        if exit_qty <= 0:
            return

        entry_price = position_entry_price(position)
        side = position_side(position)
        if side == "BUY":
            pnl = (float(exit_price) - float(entry_price)) * exit_qty
            self.cash += float(exit_price) * exit_qty
        else:
            pnl = (float(entry_price) - float(exit_price)) * exit_qty
            self.cash -= float(exit_price) * exit_qty

        estimated_charges = self._estimate_transaction_charges(
            symbol=symbol,
            side=side,
            entry_price=float(entry_price),
            exit_price=float(exit_price),
            quantity=int(exit_qty),
        )
        position["realized_pnl_parts"] = float(position.get("realized_pnl_parts") or 0.0) + float(pnl)
        position["realized_charges_parts"] = float(position.get("realized_charges_parts") or 0.0) + float(estimated_charges)
        partial_events = list(position.get("partial_exit_events") or [])
        partial_events.append(
            {
                "time": str(timestamp),
                "reason": str(action["reason"]),
                "quantity": int(exit_qty),
                "exit_price": float(exit_price),
                "pnl": float(pnl),
                "estimated_charges": float(estimated_charges),
            }
        )
        position["partial_exit_events"] = partial_events
        position["quantity"] = int(position_quantity(position)) - int(exit_qty)
        if hasattr(self.engine_helper, "apply_runner_partial_exit"):
            self.engine_helper.apply_runner_partial_exit(position, action, float(exit_price), snapshot)
        if int(position["quantity"]) <= 0:
            self._exit_position(symbol, float(exit_price), timestamp, str(action["reason"]))

    def _exit_position(self, symbol, exit_price, timestamp, reason):
        position = self.positions.pop(symbol)
        quantity = position_quantity(position)
        entry_price = position_entry_price(position)
        side = position_side(position)
        pnl, _ = calculate_position_pnl(position, exit_price)

        estimated_charges = self._estimate_transaction_charges(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            exit_price=float(exit_price),
            quantity=quantity,
        )
        net_pnl = pnl - estimated_charges
        realized_pnl_parts = float(position.get("realized_pnl_parts") or 0.0)
        realized_charges_parts = float(position.get("realized_charges_parts") or 0.0)
        total_pnl = float(pnl) + realized_pnl_parts
        total_estimated_charges = float(estimated_charges) + realized_charges_parts
        total_net_pnl = total_pnl - total_estimated_charges

        if side == "BUY":
            self.cash += exit_price * quantity
        else:
            self.cash -= exit_price * quantity

        for trade in reversed(self.trades):
            if trade["symbol"] == symbol and "exit_time" not in trade:
                trade["exit_time"] = timestamp
                trade["exit_price"] = exit_price
                trade["exit_reason"] = reason
                trade["pnl"] = total_pnl
                trade["estimated_charges"] = total_estimated_charges
                trade["net_pnl"] = total_net_pnl
                trade["partial_exit_count"] = len(position.get("partial_exit_events") or [])
                trade["partial_exit_events"] = position.get("partial_exit_events") or []
                self._on_trade({
                    "symbol": symbol,
                    "side": side,
                    "entry_time": str(trade.get("entry_time", "")),
                    "exit_time": str(timestamp),
                    "entry_price": round(float(entry_price), 2),
                    "exit_price": round(float(exit_price), 2),
                    "quantity": quantity,
                    "pnl": round(float(total_pnl), 2),
                    "charges": round(float(total_estimated_charges), 2),
                    "net_pnl": round(float(total_net_pnl), 2),
                    "exit_reason": reason,
                })
                break

    def _estimate_transaction_charges(
        self,
        *,
        symbol,
        side,
        entry_price,
        exit_price,
        quantity,
    ):
        if not TRANSACTION_COST_MODEL_ENABLED:
            return 0.0

        if (
            self.config.engine_name == "intraday_equity"
            and symbol.endswith(".NS")
            and ":" not in symbol
        ):
            breakdown = estimate_intraday_equity_round_trip_cost(
                entry_side=side,
                entry_price=float(entry_price),
                exit_price=float(exit_price),
                quantity=int(quantity),
                slippage_pct_per_side=float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
            )
            return float(breakdown.total)

        if (
            self.config.engine_name == "delivery_equity"
            and symbol.endswith(".NS")
            and ":" not in symbol
        ):
            breakdown = estimate_delivery_equity_round_trip_cost(
                entry_side=side,
                entry_price=float(entry_price),
                exit_price=float(exit_price),
                quantity=int(quantity),
                slippage_pct_per_side=float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
            )
            return float(breakdown.total)

        if self.config.engine_name in {"futures_equity", "intraday_futures"}:
            breakdown = estimate_futures_round_trip_cost(
                entry_side=side,
                entry_price=float(entry_price),
                exit_price=float(exit_price),
                quantity=int(quantity),
                slippage_pct_per_side=float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
            )
            return float(breakdown.total)

        if self.config.engine_name in {"options_equity", "intraday_options"}:
            breakdown = estimate_options_round_trip_cost(
                entry_side=side,
                entry_price=float(entry_price),
                exit_price=float(exit_price),
                quantity=int(quantity),
                slippage_pct_per_side=float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
            )
            return float(breakdown.total)

        return 0.0

    @staticmethod
    def _resolve_exit_fill_price(*, position, latest_candle, exit_reason, fallback_close):
        normalized_reason = str(exit_reason or "")
        side = position_side(position)
        candle_open = float(latest_candle.get("Open", fallback_close))
        fallback_close = float(fallback_close)

        if normalized_reason == "STOP_LOSS":
            level = float(position["stop_loss"])
            if side == "BUY":
                return min(candle_open, level) if candle_open <= level else level
            return max(candle_open, level) if candle_open >= level else level

        if normalized_reason == "TRAILING_STOP":
            level = float(position["trailing_stop"])
            if side == "BUY":
                return min(candle_open, level) if candle_open <= level else level
            return max(candle_open, level) if candle_open >= level else level

        if normalized_reason == "TARGET":
            level = float(position["target"])
            if side == "BUY":
                return max(candle_open, level) if candle_open >= level else level
            return min(candle_open, level) if candle_open <= level else level

        return fallback_close

    def _close_all_open_positions(self, history, timestamp):
        for symbol in list(self.positions):
            latest_df = history[symbol].loc[:timestamp]
            if latest_df.empty:
                continue
            exit_price = float(latest_df.iloc[-1]["Close"])
            self._exit_position(symbol, exit_price, timestamp, "END_OF_TEST")

    def _square_off_open_positions(self, history, timestamp, latest_prices):
        for symbol in list(self.positions):
            if symbol in latest_prices:
                exit_price = float(latest_prices[symbol])
            else:
                latest_df = history[symbol].loc[:timestamp]
                if latest_df.empty:
                    continue
                exit_price = float(latest_df.iloc[-1]["Close"])
            self._exit_position(symbol, exit_price, timestamp, "INTRADAY_SQUARE_OFF")

    def _current_equity(self, latest_prices):
        market_value = 0.0
        for symbol, position in self.positions.items():
            price = latest_prices.get(symbol, position_entry_price(position))
            market_value += signed_position_value(position, price)
        return self.cash + market_value

    def _mark_equity(self, timestamp, latest_prices):
        equity = self._current_equity(latest_prices)
        self.equity_curve.append(
            {
                "timestamp": timestamp,
                "equity": equity,
                "cash": self.cash,
                "open_positions": len(self.positions),
            }
        )

    def _build_summary(self):
        equity_df = pd.DataFrame(self.equity_curve)
        trades_df = pd.DataFrame(self.trades)
        if not trades_df.empty:
            trades_df = self._enrich_trade_report_fields(trades_df)
        ending_equity = float(equity_df["equity"].iloc[-1]) if not equity_df.empty else self.cash
        total_return = ((ending_equity - self.config.capital) / self.config.capital) * 100

        closed_trades = trades_df.dropna(subset=["exit_time"]) if not trades_df.empty else trades_df
        win_rate = 0.0
        max_drawdown = 0.0
        if not equity_df.empty:
            rolling_peak = equity_df["equity"].cummax()
            drawdown = (equity_df["equity"] - rolling_peak) / rolling_peak
            max_drawdown = abs(float(drawdown.min() * 100))
        if not closed_trades.empty:
            win_rate = float((closed_trades["pnl"] > 0).mean() * 100)

        total_estimated_charges = float(
            closed_trades["estimated_charges"].fillna(0.0).sum()
        ) if not closed_trades.empty and "estimated_charges" in closed_trades.columns else 0.0
        total_gross_pnl = float(
            closed_trades["pnl"].fillna(0.0).sum()
        ) if not closed_trades.empty and "pnl" in closed_trades.columns else 0.0
        total_net_pnl = float(
            closed_trades["net_pnl"].fillna(closed_trades["pnl"]).sum()
        ) if not closed_trades.empty and "pnl" in closed_trades.columns else 0.0

        tick_fills = int(
            trades_df["tick_entry"].sum()
            if not trades_df.empty and "tick_entry" in trades_df.columns
            else 0
        )
        return {
            "config": self.config,
            "ending_equity": ending_equity,
            "total_return_percent": total_return,
            "closed_trades": int(len(closed_trades)),
            "win_rate_percent": win_rate,
            "max_drawdown_percent": max_drawdown,
            "total_estimated_charges": total_estimated_charges,
            "total_gross_pnl": total_gross_pnl,
            "total_net_pnl": total_net_pnl,
            "tick_entry_fills": tick_fills,
            "equity_curve": equity_df,
            "trades": trades_df,
        }

    @staticmethod
    def _enrich_trade_report_fields(trades_df):
        def parse_symbol_metadata(symbol):
            match = re.search(r"(\d+)(CE|PE)$", str(symbol or ""))
            if not match:
                return None, None
            return int(match.group(1)), match.group(2)

        trades_df = trades_df.copy()
        if "strike" not in trades_df.columns:
            trades_df["strike"] = None
        if "option_type" not in trades_df.columns:
            trades_df["option_type"] = None
        if "underlying_close_at_entry" not in trades_df.columns:
            trades_df["underlying_close_at_entry"] = None

        for index, row in trades_df.iterrows():
            parsed_strike, parsed_option_type = parse_symbol_metadata(row.get("symbol"))
            if pd.isna(row.get("strike")) and parsed_strike is not None:
                trades_df.at[index, "strike"] = parsed_strike
            if pd.isna(row.get("option_type")) and parsed_option_type is not None:
                trades_df.at[index, "option_type"] = parsed_option_type
            if pd.isna(row.get("underlying_close_at_entry")):
                analytics = row.get("analytics")
                if isinstance(analytics, dict):
                    trades_df.at[index, "underlying_close_at_entry"] = analytics.get("underlying_price")

        return trades_df


def print_prompt_help(explanation, example=None):
    message = explanation.strip()
    if example:
        message += f" Example: {example}"
    print(f"[HELP] {message}")


def prompt_choice(message, valid_choices, default=None):
    normalized = {
        str(choice["key"]): choice["value"]
        for choice in valid_choices
    }
    while True:
        raw = input(message).strip()
        if not raw and default is not None:
            raw = str(default)
        if raw in normalized:
            return normalized[raw]
        print("Please choose a valid option.")


def prompt_int(message, default=None, minimum=None, maximum=None):
    while True:
        raw = input(message).strip()
        if not raw and default is not None:
            value = int(default)
        else:
            try:
                value = int(raw)
            except ValueError:
                print("Enter a valid whole number.")
                continue
        if minimum is not None and value < minimum:
            print(f"Value must be at least {minimum}.")
            continue
        if maximum is not None and value > maximum:
            print(f"Value must be at most {maximum}.")
            continue
        return value


def prompt_float(message, default=None, minimum=None):
    while True:
        raw = input(message).strip()
        if not raw and default is not None:
            value = float(default)
        else:
            try:
                value = float(raw)
            except ValueError:
                print("Enter a valid number.")
                continue
        if minimum is not None and value < minimum:
            print(f"Value must be at least {minimum}.")
            continue
        return value


def prompt_symbol_selection():
    print_prompt_help(
        "Choose how many equity symbols you want to backtest.",
        "3 for full NIFTY50 universe, or 1 for one stock only",
    )
    mode = prompt_choice(
        f"Symbol mode: SINGLE(1), MANUAL MULTI(2), NIFTY50 UNIVERSE(3)? [default {_prompt_default_int('symbol_mode', 3)}]: ",
        [
            {"key": 1, "value": "SINGLE"},
            {"key": 2, "value": "MANUAL_MULTI"},
            {"key": 3, "value": "NIFTY50"},
        ],
        default=_prompt_default_int("symbol_mode", 3),
    )

    if mode == "NIFTY50":
        return tuple(NIFTY50_SYMBOLS), "NIFTY50 universe"

    if mode == "SINGLE":
        print_prompt_help(
            "Pick one stock from the shortcut table below.",
            "11 for RPOWER.NS",
        )
        for key, value in SINGLE_SYMBOL_TABLE.items():
            print(f"{key}. {value}")
        selection = prompt_choice(
            f"Choose single symbol table entry [default {_prompt_default('single_symbol_key', '1')}]: ",
            [{"key": key, "value": value} for key, value in SINGLE_SYMBOL_TABLE.items()],
            default=_prompt_default("single_symbol_key", "1"),
        )
        return (selection,), f"Single symbol {selection}"

    print_prompt_help(
        "Choose multiple stocks by entering table keys separated by commas.",
        "11,12,13",
    )
    for key, value in MANUAL_SYMBOL_TABLE.items():
        print(f"{key}. {value}")
    raw = input("Choose manual symbols by comma-separated keys [example 1,3,5]: ").strip()
    keys = [item.strip() for item in raw.split(",") if item.strip()]
    symbols = []
    for key in keys:
        symbol = MANUAL_SYMBOL_TABLE.get(key)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        symbols = [MANUAL_SYMBOL_TABLE["1"]]
    return tuple(symbols), f"Manual symbols {', '.join(symbols)}"


def prompt_fno_base_symbol(engine_name):
    choices = _supported_backtest_fno_choices()
    if not choices:
        raise RuntimeError(
            "No configured F&O underlyings have a supported backtest proxy."
        )
    if engine_name in {"futures_equity", "intraday_futures"}:
        print_prompt_help(
            "Choose the index universe for futures backtesting.",
            "3 for both configured underlyings" if len(choices) > 1 else "1 for the only configured underlying",
        )
        default_choice = _prompt_default_int("fno_futures_base_symbol", 3 if len(choices) > 1 else 1)
        prompt_choices = [
            {"key": item["index"], "value": item["symbol"]}
            for item in choices
        ]
        if len(choices) > 1:
            prompt_choices.append({"key": len(choices) + 1, "value": "BOTH"})
        label = ", ".join(
            f"{item['display_name']}({item['index']})"
            for item in choices
        )
        if len(choices) > 1:
            label += f", BOTH({len(choices) + 1})"
        return prompt_choice(
            f"F&O futures universe: {label} [default {default_choice}]: ",
            prompt_choices,
            default=default_choice,
        )

    print_prompt_help(
        "Choose the options underlying you want to simulate.",
        f"1 for {choices[0]['display_name']}",
    )
    default_choice = _prompt_default_int("fno_options_base_symbol", 1)
    return prompt_choice(
        "F&O options underlying: "
        + ", ".join(f"{item['display_name']}({item['index']})" for item in choices)
        + f" [default {default_choice}]: ",
        [{"key": item["index"], "value": item["symbol"]} for item in choices],
        default=default_choice,
    )


def prompt_fno_expiry(base_symbol, instrument_type):
    expiries = get_available_expiries(base_symbol, instrument_type=instrument_type)
    if not expiries:
        raise RuntimeError(f"No expiries found for {base_symbol}.")
    print(f"[SETUP] Available expiries for {get_fno_display_name(base_symbol)}:")
    for idx, expiry in enumerate(expiries, start=1):
        print(f"[SETUP]   {idx}. {expiry}")
    print("[SETUP] Choose expiry or press Enter to use the nearest available expiry")
    print_prompt_help(
        "Enter the serial number of the expiry from the list above.",
        "1",
    )
    expiry_default = _prompt_default_int("expiry_choice", 1)
    choice = prompt_int(
        f"Choose expiry [default {expiry_default}]: ",
        default=expiry_default,
        minimum=1,
        maximum=len(expiries),
    )
    return expiries[choice - 1]


def prompt_option_pair_strikes(base_symbol, expiry):
    pe_strikes = get_available_option_strikes(base_symbol, expiry, "PE")
    ce_strikes = get_available_option_strikes(base_symbol, expiry, "CE")
    if not pe_strikes or not ce_strikes:
        raise RuntimeError(f"No option strikes found for {base_symbol} {expiry}.")
    print(f"[SETUP] PE strikes sample: {pe_strikes[:8]}")
    print_prompt_help(
        "Enter the lower PE strike for the bounded range pair.",
        str(pe_strikes[min(1, len(pe_strikes) - 1)]),
    )
    lower_strike = prompt_int("Enter lower PE strike: ", minimum=1)
    print(f"[SETUP] CE strikes sample: {ce_strikes[:8]}")
    print_prompt_help(
        "Enter the upper CE strike for the bounded range pair.",
        str(ce_strikes[min(1, len(ce_strikes) - 1)]),
    )
    upper_strike = prompt_int("Enter upper CE strike: ", minimum=1)
    if lower_strike >= upper_strike:
        raise RuntimeError("For a range pair, lower PE strike must be below upper CE strike.")
    return lower_strike, upper_strike


def build_fno_backtest_universe(engine_name, base_symbol):
    if base_symbol == "BOTH":
        return tuple(BACKTEST_FNO_SYMBOLS.values())
    return (BACKTEST_FNO_SYMBOLS[base_symbol],)


def prompt_fno_contract_selection(engine_name):
    selection = prompt_fno_base_symbol(engine_name)
    summary_lines = []
    contract_settings = None

    if selection == "BOTH":
        summary_lines.append("F&O futures universe: NIFTY 50 + SENSEX")
        return build_fno_backtest_universe(engine_name, selection), summary_lines, contract_settings

    expiry = prompt_fno_expiry(selection, "OPT" if "options" in engine_name else "FUT")
    if engine_name == "intraday_options":
        summary_lines.append(
            f"{get_fno_display_name(selection)} | Expiry={expiry} | Underlying signal source={get_fno_spot_quote_symbol(selection)}"
        )
    else:
        summary_lines.append(
            f"{get_fno_display_name(selection)} | Expiry={expiry} | Backtest proxy={BACKTEST_FNO_SYMBOLS[selection]}"
        )

    if engine_name == "intraday_options":
        print_prompt_help(
            "Choose whether to simulate dynamic ATM selection or a fixed two-leg range pair.",
            "1 for ATM single option",
        )
        structure_mode = prompt_choice(
            f"Options structure: ATM SINGLE OPTION(1) or TWO-LEG RANGE PAIR(2) [default {_prompt_default_int('intraday_options_structure_mode', 1)}]: ",
            [
                {"key": 1, "value": "SINGLE"},
                {"key": 2, "value": "PAIR"},
            ],
            default=_prompt_default_int("intraday_options_structure_mode", 1),
        )
        if structure_mode == "SINGLE":
            print_prompt_help(
                "Choose how far from ATM the dynamic strike should be selected.",
                "1 for ATM, 2 for ATM + 1 strike",
            )
            default_strike_mode = int(_prompt_default("intraday_options_strike_mode", 1))
            strike_mode = prompt_choice(
                f"ATM strike mode: ATM(1), ATM + 1 STRIKE(2), ATM - 1 STRIKE(3) [default {default_strike_mode}]: ",
                [
                    {"key": 1, "value": "ATM"},
                    {"key": 2, "value": "ATM_PLUS_1"},
                    {"key": 3, "value": "ATM_MINUS_1"},
                ],
                default=default_strike_mode,
            )
            summary_lines.append(
                f"ATM dynamic structure | Underlying={selection} | Expiry={expiry} | Strike mode={strike_mode.replace('_', ' ')}"
            )
            contract_settings = {
                "base_symbol": selection,
                "expiry": expiry,
                "structure_mode": "SINGLE",
                "strike_mode": strike_mode,
                "underlying_symbol": get_fno_spot_quote_symbol(selection),
            }
        else:
            lower_strike, upper_strike = prompt_option_pair_strikes(selection, expiry)
            summary_lines.append(
                f"Two-leg range pair | Underlying={selection} | Expiry={expiry} | Range={lower_strike}-{upper_strike}"
            )
            contract_settings = {
                "base_symbol": selection,
                "expiry": expiry,
                "structure_mode": "PAIR",
                "lower_strike": lower_strike,
                "upper_strike": upper_strike,
                "underlying_symbol": get_fno_spot_quote_symbol(selection),
            }

        print("[SETUP] Selected F&O contract summary:")
        for line in summary_lines:
            print(f"[SETUP]   {line}")
        confirm = prompt_choice(
            f"Continue with these F&O contracts? YES(1) or NO(2) [default {_prompt_default_int('fno_contract_confirm', 1)}]: ",
            [{"key": 1, "value": "YES"}, {"key": 2, "value": "NO"}],
            default=_prompt_default_int("fno_contract_confirm", 1),
        )
        if confirm != "YES":
            raise SystemExit("F&O backtest selection cancelled.")

    return build_fno_backtest_universe(engine_name, selection), summary_lines, contract_settings


def prompt_multi_strategy_selection(strategy_options):
    print("Choose strategies:")
    for key, value in strategy_options.items():
        print(f"{key}. {value}")
    print_prompt_help(
        "Enter one or more strategy numbers separated by commas.",
        "1,2,4 for MA, RSI, BREAKOUT",
    )
    raw = input("Enter comma-separated strategy numbers: ").strip()
    selected_keys = [item.strip() for item in raw.split(",") if item.strip()]
    if not selected_keys:
        selected_keys = [
            key
            for key, value in strategy_options.items()
            if value in {"MA", "RSI", "BREAKOUT"}
        ] or [next(iter(strategy_options))]
    selected = []
    for key in selected_keys:
        value = strategy_options.get(key)
        if value and value not in selected:
            selected.append(value)
    return tuple(selected)


def prompt_strategy_setup(engine_class):
    if engine_class.name == "intraday_options":
        print_prompt_help(
            "Choose the intraday options strategy to backtest.",
            "3 for VWAP Reversion",
        )
        strategy_name = prompt_choice(
            f"Intraday options strategy: Momentum(1), ORB(2), VWAP Reversion(3), Multi-strategy(4), Breakout Expansion(5), IV Expansion(6), Trap Reversal(7) [default {_prompt_default_int('intraday_options_strategy', 1)}]: ",
            [
                {"key": 1, "value": "ATM_MOMENTUM"},
                {"key": 2, "value": "ATM_ORB"},
                {"key": 3, "value": "ATM_VWAP_REVERSION"},
                {"key": 4, "value": "ATM_MULTI"},
                {"key": 5, "value": "ATM_BREAKOUT_EXPANSION"},
                {"key": 6, "value": "ATM_IV_EXPANSION"},
                {"key": 7, "value": "ATM_TRAP_REVERSAL"},
            ],
            default=_prompt_default_int("intraday_options_strategy", 1),
        )
        return "SINGLE", strategy_name, (strategy_name,), 1

    if engine_class.name == "intraday_equity":
        print_prompt_help(
            "Choose whether to run one strategy, a combination, or auto-adaptive intraday equity mode.",
            "2 for Multi",
        )
        mode = prompt_choice(
            f"Strategy mode: Single(1), Multi(2), Auto Adaptive(3) [default {_prompt_default_int('intraday_equity_strategy_mode', 2)}]: ",
            [
                {"key": 1, "value": "SINGLE"},
                {"key": 2, "value": "MULTI"},
                {"key": 3, "value": "AUTO_ADAPTIVE"},
            ],
            default=_prompt_default_int("intraday_equity_strategy_mode", 2),
        )
        if mode == "AUTO_ADAPTIVE":
            return (
                "AUTO_ADAPTIVE",
                None,
                ("MA", "RSI"),
                max(2, int(INTRADAY_EQUITY_AUTO_NORMAL_MIN_CONFIRMATIONS)),
            )
    else:
        print_prompt_help(
            "Choose whether to run one strategy or combine multiple strategies.",
            "2 for Multi",
        )
        mode = prompt_choice(
            f"Strategy mode: Single(1) or Multi(2) [default {_prompt_default_int('default_strategy_mode', 2)}]: ",
            [
                {"key": 1, "value": "SINGLE"},
                {"key": 2, "value": "MULTI"},
            ],
            default=_prompt_default_int("default_strategy_mode", 2),
        )

    if mode == "SINGLE":
        print("Available strategies:")
        for key, value in engine_class.supported_strategies.items():
            print(f"{key}. {value}")
        print_prompt_help(
            "Choose one strategy number from the list for this engine.",
            "1",
        )
        strategy_name = prompt_choice(
            "Choose strategy: ",
            [
                {"key": key, "value": value}
                for key, value in engine_class.supported_strategies.items()
            ],
            default=_prompt_default("single_strategy_key", "1"),
        )
        return "SINGLE", strategy_name, (strategy_name,), 1

    strategies = prompt_multi_strategy_selection(engine_class.supported_strategies)
    print_prompt_help(
        "Minimum confirmations means how many chosen strategies must agree before entry.",
        "2",
    )
    min_confirmations = prompt_int(
        f"Minimum confirmations [default {min(2, len(strategies))}]: ",
        default=min(2, len(strategies)),
        minimum=1,
        maximum=len(strategies),
    )
    return "MULTI", None, strategies, min_confirmations


def prompt_backtest_config():
    print_prompt_help(
        "Choose which trading engine you want to backtest.",
        "6 for INTRADAY OPTIONS",
    )
    engine_choice = prompt_choice(
        f"Trading engine: INTRADAY EQUITY(1), DELIVERY EQUITY(2), FUTURES EQUITY(3), OPTIONS EQUITY(4), INTRADAY FUTURES(5), INTRADAY OPTIONS(6) [default {_prompt_default_int('engine_choice', 1)}]: ",
        [
            {"key": 1, "value": "1"},
            {"key": 2, "value": "2"},
            {"key": 3, "value": "3"},
            {"key": 4, "value": "4"},
            {"key": 5, "value": "5"},
            {"key": 6, "value": "6"},
        ],
        default=_prompt_default_int("engine_choice", 1),
    )
    engine_class = ENGINE_OPTIONS[engine_choice]

    print_prompt_help(
        "Enter total capital available for this backtest run.",
        "100000",
    )
    capital_default = _prompt_default_float("capital", 100000)
    capital = prompt_float("Enter capital for backtest: ", default=capital_default, minimum=1)

    if "futures" in engine_class.name or "options" in engine_class.name:
        universe, summary_lines, option_backtest_settings = prompt_fno_contract_selection(engine_class.name)
    else:
        universe, summary_label = prompt_symbol_selection()
        summary_lines = [summary_label]
        option_backtest_settings = None

    intraday_options_lot_mode = str(get_runtime_config().fno.intraday_options_lot_mode or "CAPITAL_BASED").upper()
    intraday_options_entry_mode = str(get_runtime_config().fno.intraday_options_entry_mode or "LIVE_STAGED").upper()
    if engine_class.name == "intraday_options":
        summary_lines.append(
            "Intraday options lot sizing="
            + ("1 lot default" if intraday_options_lot_mode == "ONE_LOT" else "capital based")
            + " (from config)"
        )
        summary_lines.append(
            "Intraday options entry mode="
            + (
                "live staged breakout confirmation"
                if intraday_options_entry_mode == "LIVE_STAGED"
                else "legacy immediate breakout"
            )
            + " (from config)"
        )

    print_prompt_help(
        "Choose the risk style that controls ATR stop, trailing stop, and capital risk.",
        "2 for BALANCED",
    )
    risk_styles = get_risk_style_presets(engine_class.name)
    risk_style = risk_styles[
        prompt_choice(
            f"Risk style: CONSERVATIVE(1), BALANCED(2), AGGRESSIVE(3)? [default {_prompt_default_int('risk_style', 2)}]: ",
            [
                {"key": 1, "value": "1"},
                {"key": 2, "value": "2"},
                {"key": 3, "value": "3"},
            ],
            default=_prompt_default_int("risk_style", 2),
        )
    ]

    print_prompt_help(
        "Enter how many positions can be open at the same time.",
        "1",
    )
    max_positions_default = _prompt_default_int("max_positions", 1)
    max_positions = prompt_int(
        f"Max open positions [default {max_positions_default}]: ",
        default=max_positions_default,
        minimum=1,
    )
    print_prompt_help(
        "Enter the maximum capital allowed per trade.",
        f"{capital / max_positions:.0f}",
    )
    max_capital_per_trade = prompt_float(
        f"Max capital per trade [default {capital / max_positions:.2f}]: ",
        default=capital / max_positions,
        minimum=1,
    )
    print_prompt_help(
        "Enter the maximum total capital that can be deployed across all open trades.",
        f"{capital:.0f}",
    )
    max_capital_deployed = prompt_float(
        f"Max capital deployed [default {capital:.2f}]: ",
        default=capital,
        minimum=1,
    )
    print_prompt_help(
        "Choose whether the same symbol can be traded more than once per day.",
        "1 for YES",
    )
    one_trade_per_symbol_per_day = (
        prompt_choice(
            f"One trade per symbol per day? YES(1) or NO(2) [default {_prompt_default_int('one_trade_per_symbol_per_day', 1)}]: ",
            [{"key": 1, "value": "YES"}, {"key": 2, "value": "NO"}],
            default=_prompt_default_int("one_trade_per_symbol_per_day", 1),
        )
        == "YES"
    )

    print_prompt_help(
        "Choose whether to take only the top signal or multiple ranked signals.",
        "1 for TOP 1, 2 for TOP N",
    )
    entry_selection_mode = prompt_choice(
        f"Entry selection: TOP 1(1) or TOP N(2)? [default {_prompt_default_int('entry_selection_mode', 1)}]: ",
        [{"key": 1, "value": "TOP_1"}, {"key": 2, "value": "TOP_N"}],
        default=_prompt_default_int("entry_selection_mode", 1),
    )
    top_n = 1
    if entry_selection_mode == "TOP_N":
        print_prompt_help(
            "Enter how many top-ranked signals should be entered each cycle.",
            "2",
        )
        top_n_default = _prompt_default_int("top_n", 2)
        top_n = prompt_int(
            f"How many top-ranked entries? [default {top_n_default}]: ",
            default=top_n_default,
            minimum=1,
        )

    strategy_mode, strategy_name, strategies, min_confirmations = prompt_strategy_setup(engine_class)

    default_period, default_interval = BACKTEST_DEFAULT_DATA.get(
        engine_class.name,
        (getattr(engine_class, "data_period", "6mo"), getattr(engine_class, "data_interval", "1d")),
    )
    if engine_class.name == "intraday_options":
        print(
            "[SETUP] Configured intraday-options backtest defaults: "
            f"period={default_period} and interval={default_interval}."
        )
    print(f"[SETUP] Valid backtest periods: {', '.join(VALID_BACKTEST_PERIODS)}")
    print_prompt_help(
        "Enter the Yahoo Finance period window for data download.",
        default_period,
    )
    period = input(f"Backtest period [default {default_period}]: ").strip() or default_period
    print(f"[SETUP] Valid backtest intervals: {', '.join(VALID_BACKTEST_INTERVALS)}")
    print_prompt_help(
        "Enter the Yahoo Finance candle interval for the backtest.",
        default_interval,
    )
    interval = input(f"Backtest interval [default {default_interval}]: ").strip() or default_interval

    summary_lines.extend(
        [
            f"Engine={engine_class.name}",
            f"Universe={', '.join(universe)}",
            f"Risk style={risk_style['name']}",
            f"Strategy mode={strategy_mode}",
            f"Strategies={', '.join(strategies)}",
            f"Period={period} | Interval={interval}",
        ]
    )

    if engine_class.name == "intraday_options":
        if (option_backtest_settings or {}).get("structure_mode") == "SINGLE":
            summary_lines.append(
                "Note: intraday options backtest now resolves real option contracts and uses option premium candles for entry, exit, and sizing."
            )
        else:
            summary_lines.append(
                "Note: intraday options premium backtest currently supports only ATM SINGLE OPTION structure."
            )

    return BacktestConfig(
        engine_name=engine_class.name,
        capital=capital,
        period=period,
        interval=interval,
        strategy_mode=strategy_mode,
        strategy_name=strategy_name,
        strategies=strategies,
        min_confirmations=min_confirmations,
        risk_percent=risk_style["risk_percent"],
        atr_stop_multiplier=risk_style["atr_stop_multiplier"],
        trailing_atr_multiplier=risk_style["trailing_atr_multiplier"],
        target_risk_reward=risk_style["target_risk_reward"],
        risk_style_name=risk_style["name"],
        top_n=max(1, top_n),
        max_positions=max_positions,
        max_capital_per_trade=max_capital_per_trade,
        max_capital_deployed=max_capital_deployed,
        universe=universe,
        one_trade_per_symbol_per_day=one_trade_per_symbol_per_day,
        intraday_options_lot_mode=intraday_options_lot_mode,
        intraday_options_entry_mode=intraday_options_entry_mode,
        summary_lines=summary_lines,
        option_backtest_settings=option_backtest_settings,
    )


def export_backtest_results(summary):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    config = summary["config"]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"backtest_{config.engine_name}_{timestamp}"
    summary_path = RESULTS_DIR / f"{prefix}_summary.txt"
    trades_path = RESULTS_DIR / f"{prefix}_trades.csv"
    equity_path = RESULTS_DIR / f"{prefix}_equity.csv"

    lines = ["Backtest summary"]
    lines.extend(config.summary_lines)
    lines.append(f"Ending equity: {summary['ending_equity']:.2f}")
    lines.append(f"Total return: {summary['total_return_percent']:.2f}%")
    lines.append(f"Closed trades: {summary['closed_trades']}")
    lines.append(f"Win rate: {summary['win_rate_percent']:.2f}%")
    lines.append(f"Max drawdown: {summary['max_drawdown_percent']:.2f}%")
    lines.append(f"Estimated charges: {summary['total_estimated_charges']:.2f}")
    lines.append(f"Estimated net P&L: {summary['total_net_pnl']:+.2f}")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary["trades"].to_csv(trades_path, index=False)
    summary["equity_curve"].to_csv(equity_path, index=False)

    return summary_path, trades_path, equity_path


def print_summary(summary):
    from display import backtest_summary as _backtest_summary, fmt_header
    _backtest_summary(summary)
    summary_path, trades_path, equity_path = export_backtest_results(summary)
    print(f"\n{fmt_header('  Results saved to:')}")
    print(f"  Summary    {summary_path}")
    print(f"  Trades CSV {trades_path}")
    print(f"  Equity CSV {equity_path}")


if __name__ == "__main__":
    summary = BacktestEngine(prompt_backtest_config()).run()
    print_summary(summary)
