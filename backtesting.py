from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from config import (
    INTRADAY_EQUITY_AUTO_NORMAL_MIN_CONFIRMATIONS,
    MANUAL_SYMBOL_TABLE,
    NIFTY50_SYMBOLS,
    SINGLE_SYMBOL_TABLE,
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
from engines.common import build_position, evaluate_exit, update_trailing_stop
from fno_data_fetcher import get_available_expiries, get_available_option_strikes, get_fno_display_name
from models.position_adapter import (
    calculate_position_pnl,
    position_entry_price,
    position_quantity,
    position_side,
    signed_position_value,
)
from risk_manager import atr_position_size, atr_stop_from_value, calculate_target_price
from signal_scoring import evaluate_symbol_signal, get_atr_value, rank_candidates
from transaction_costs import estimate_intraday_equity_round_trip_cost
from transaction_costs import (
    estimate_delivery_equity_round_trip_cost,
    estimate_futures_round_trip_cost,
    estimate_options_round_trip_cost,
)


RISK_STYLES = {
    "1": {
        "name": "CONSERVATIVE",
        "atr_stop_multiplier": 1.5,
        "trailing_atr_multiplier": 1.0,
        "target_risk_reward": 1.8,
        "sl_percent": 0.4,
        "target_percent": 0.8,
        "trailing_percent": 0.25,
        "risk_percent": 0.005,
    },
    "2": {
        "name": "BALANCED",
        "atr_stop_multiplier": 2.0,
        "trailing_atr_multiplier": 1.25,
        "target_risk_reward": 2.0,
        "sl_percent": 0.5,
        "target_percent": 1.0,
        "trailing_percent": 0.35,
        "risk_percent": 0.01,
    },
    "3": {
        "name": "AGGRESSIVE",
        "atr_stop_multiplier": 2.5,
        "trailing_atr_multiplier": 1.5,
        "target_risk_reward": 2.2,
        "sl_percent": 0.7,
        "target_percent": 1.4,
        "trailing_percent": 0.5,
        "risk_percent": 0.015,
    },
}

ENGINE_OPTIONS = {
    "1": IntradayEquityEngine,
    "2": DeliveryEquityEngine,
    "3": FuturesEquityEngine,
    "4": OptionsEquityEngine,
    "5": IntradayFuturesEngine,
    "6": IntradayOptionsEngine,
}

BACKTEST_FNO_SYMBOLS = {
    "NIFTY": "^NSEI",
    "SENSEX": "^BSESN",
}
BACKTEST_DEFAULT_DATA = {
    "intraday_equity": ("5d", "5m"),
    "delivery_equity": ("6mo", "1d"),
    "futures_equity": ("2mo", "15m"),
    "options_equity": ("2mo", "15m"),
    "intraday_futures": ("5d", "5m"),
    "intraday_options": ("5d", "5m"),
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
    top_n: int
    max_positions: int
    max_capital_per_trade: float
    max_capital_deployed: float
    universe: tuple[str, ...]
    one_trade_per_symbol_per_day: bool = True
    summary_lines: list[str] = field(default_factory=list)


class BacktestEngine:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.cash = config.capital
        self.positions = {}
        self.trades = []
        self.equity_curve = []
        self.traded_symbols_by_day = defaultdict(set)
        self.engine_helper = self._build_engine_helper()

    def _build_engine_helper(self):
        sl_percent = 0.5
        target_percent = 1.0
        trailing_percent = 0.35
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
            return IntradayOptionsEngine(sl_percent, target_percent, trailing_percent)
        return None

    def fetch_history(self):
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
                history[symbol] = data.sort_index()
        return history

    def run(self):
        history = self.fetch_history()
        if not history:
            raise RuntimeError("No historical data fetched for the selected universe.")

        timeline = sorted({index for df in history.values() for index in df.index})
        for timestamp in timeline:
            self._process_timestamp(history, timestamp)

        self._close_all_open_positions(history, timeline[-1])
        return self._build_summary()

    def _process_timestamp(self, history, timestamp):
        latest_prices = {}
        candidates = []

        for symbol, df in history.items():
            current_slice = df.loc[:timestamp]
            if current_slice.empty:
                continue

            latest_candle = current_slice.iloc[-1]
            latest_prices[symbol] = float(latest_candle["Close"])

            if current_slice.index[-1] != timestamp:
                continue

            if symbol in self.positions:
                position = self.positions[symbol]
                update_trailing_stop(position, latest_prices[symbol], 0)
                exit_reason = evaluate_exit(position, latest_candle, include_target=True)
                if exit_reason:
                    self._exit_position(symbol, latest_prices[symbol], timestamp, exit_reason)
                continue

            if not self._can_enter_symbol(symbol, timestamp):
                continue

            evaluation = self._evaluate_signal(symbol, current_slice)
            if evaluation["signal"] not in {"BUY", "SELL"}:
                continue

            candidates.append(
                {
                    "symbol": symbol,
                    "signal": evaluation["signal"],
                    "agreement_count": evaluation["agreement_count"],
                    "score": evaluation["score"],
                    "latest_close": latest_prices[symbol],
                    "atr": get_atr_value(current_slice),
                    "strategy": evaluation.get("strategy"),
                    "option_signal": evaluation.get("option_signal"),
                    "reason": evaluation.get("reason"),
                }
            )

        ranked = rank_candidates(candidates)
        self._enter_ranked_candidates(ranked, timestamp)
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

    def _enter_ranked_candidates(self, ranked_candidates, timestamp):
        for candidate in ranked_candidates[: self.config.top_n]:
            if len(self.positions) >= self.config.max_positions:
                break
            if candidate["symbol"] in self.positions:
                continue
            if candidate["atr"] <= 0:
                continue

            current_equity = self._current_equity({})
            deployed_capital = current_equity - self.cash
            remaining_deployable = max(0.0, self.config.max_capital_deployed - deployed_capital)
            if remaining_deployable <= 0:
                break

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
            if qty <= 0:
                continue

            entry_price = candidate["latest_close"]
            stop_data = atr_stop_from_value(
                candidate["signal"],
                entry_price,
                candidate["atr"],
                self.config.atr_stop_multiplier,
            )
            target_price = calculate_target_price(
                candidate["signal"],
                entry_price,
                stop_data["stop_distance"] * self.config.target_risk_reward,
            )
            trailing_distance = candidate["atr"] * self.config.trailing_atr_multiplier
            trailing_stop = (
                entry_price - trailing_distance
                if candidate["signal"] == "BUY"
                else entry_price + trailing_distance
            )

            self.positions[candidate["symbol"]] = build_position(
                symbol=candidate["symbol"],
                side=candidate["signal"],
                quantity=qty,
                entry_price=entry_price,
                stop_loss=stop_data["stop_loss_price"],
                target=target_price,
                trailing_stop=trailing_stop,
                trailing_distance=trailing_distance,
                atr=candidate["atr"],
                stop_distance=stop_data["stop_distance"],
            )

            if candidate["signal"] == "BUY":
                self.cash -= entry_price * qty
            else:
                self.cash += entry_price * qty

            trade_day = pd.Timestamp(timestamp).date()
            self.traded_symbols_by_day[trade_day].add(candidate["symbol"])
            self.trades.append(
                {
                    "symbol": candidate["symbol"],
                    "side": candidate["signal"],
                    "entry_time": timestamp,
                    "entry_price": entry_price,
                    "quantity": qty,
                    "score": candidate["score"],
                    "atr": candidate["atr"],
                    "strategy": candidate.get("strategy"),
                    "option_signal": candidate.get("option_signal"),
                    "entry_reason": candidate.get("reason"),
                }
            )

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

        if side == "BUY":
            self.cash += exit_price * quantity
        else:
            self.cash -= exit_price * quantity

        for trade in reversed(self.trades):
            if trade["symbol"] == symbol and "exit_time" not in trade:
                trade["exit_time"] = timestamp
                trade["exit_price"] = exit_price
                trade["exit_reason"] = reason
                trade["pnl"] = pnl
                trade["estimated_charges"] = estimated_charges
                trade["net_pnl"] = net_pnl
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

    def _close_all_open_positions(self, history, timestamp):
        for symbol in list(self.positions):
            latest_df = history[symbol].loc[:timestamp]
            if latest_df.empty:
                continue
            exit_price = float(latest_df.iloc[-1]["Close"])
            self._exit_position(symbol, exit_price, timestamp, "END_OF_TEST")

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
        total_net_pnl = float(
            closed_trades["net_pnl"].fillna(closed_trades["pnl"]).sum()
        ) if not closed_trades.empty and "pnl" in closed_trades.columns else 0.0

        return {
            "config": self.config,
            "ending_equity": ending_equity,
            "total_return_percent": total_return,
            "closed_trades": int(len(closed_trades)),
            "win_rate_percent": win_rate,
            "max_drawdown_percent": max_drawdown,
            "total_estimated_charges": total_estimated_charges,
            "total_net_pnl": total_net_pnl,
            "equity_curve": equity_df,
            "trades": trades_df,
        }


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
        "Symbol mode: SINGLE(1), MANUAL MULTI(2), NIFTY50 UNIVERSE(3)? [default 3]: ",
        [
            {"key": 1, "value": "SINGLE"},
            {"key": 2, "value": "MANUAL_MULTI"},
            {"key": 3, "value": "NIFTY50"},
        ],
        default=3,
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
            "Choose single symbol table entry [default 1]: ",
            [{"key": key, "value": value} for key, value in SINGLE_SYMBOL_TABLE.items()],
            default=1,
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
    if engine_name in {"futures_equity", "intraday_futures"}:
        print_prompt_help(
            "Choose the index universe for futures backtesting.",
            "3 for both NIFTY 50 and SENSEX",
        )
        return prompt_choice(
            "F&O futures universe: NIFTY 50(1), SENSEX(2), BOTH(3) [default 3]: ",
            [
                {"key": 1, "value": "NIFTY"},
                {"key": 2, "value": "SENSEX"},
                {"key": 3, "value": "BOTH"},
            ],
            default=3,
        )

    print_prompt_help(
        "Choose the options underlying you want to simulate.",
        "1 for NIFTY 50",
    )
    return prompt_choice(
        "F&O options underlying: NIFTY 50(1), SENSEX(2) [default 1]: ",
        [
            {"key": 1, "value": "NIFTY"},
            {"key": 2, "value": "SENSEX"},
        ],
        default=1,
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
    choice = prompt_int("Choose expiry [default 1]: ", default=1, minimum=1, maximum=len(expiries))
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

    if selection == "BOTH":
        summary_lines.append("F&O futures universe: NIFTY 50 + SENSEX")
        return build_fno_backtest_universe(engine_name, selection), summary_lines

    expiry = prompt_fno_expiry(selection, "OPT" if "options" in engine_name else "FUT")
    summary_lines.append(
        f"{get_fno_display_name(selection)} | Expiry={expiry} | Backtest proxy={BACKTEST_FNO_SYMBOLS[selection]}"
    )

    if engine_name == "intraday_options":
        print_prompt_help(
            "Choose whether to simulate dynamic ATM selection or a fixed two-leg range pair.",
            "1 for ATM single option",
        )
        structure_mode = prompt_choice(
            "Options structure: ATM SINGLE OPTION(1) or TWO-LEG RANGE PAIR(2)? [default 1]: ",
            [
                {"key": 1, "value": "SINGLE"},
                {"key": 2, "value": "PAIR"},
            ],
            default=1,
        )
        if structure_mode == "SINGLE":
            print_prompt_help(
                "Choose how far from ATM the dynamic strike should be selected.",
                "1 for ATM, 2 for ATM + 1 strike",
            )
            strike_mode = prompt_choice(
                "ATM strike mode: ATM(1), ATM + 1 STRIKE(2), ATM - 1 STRIKE(3) [default 1]: ",
                [
                    {"key": 1, "value": "ATM"},
                    {"key": 2, "value": "ATM_PLUS_1"},
                    {"key": 3, "value": "ATM_MINUS_1"},
                ],
                default=1,
            )
            summary_lines.append(
                f"ATM dynamic structure | Underlying={selection} | Expiry={expiry} | Strike mode={strike_mode.replace('_', ' ')}"
            )
        else:
            lower_strike, upper_strike = prompt_option_pair_strikes(selection, expiry)
            summary_lines.append(
                f"Two-leg range pair | Underlying={selection} | Expiry={expiry} | Range={lower_strike}-{upper_strike}"
            )

        print("[SETUP] Selected F&O contract summary:")
        for line in summary_lines:
            print(f"[SETUP]   {line}")
        confirm = prompt_choice(
            "Continue with these F&O contracts? YES(1) or NO(2) [default 1]: ",
            [{"key": 1, "value": "YES"}, {"key": 2, "value": "NO"}],
            default=1,
        )
        if confirm != "YES":
            raise SystemExit("F&O backtest selection cancelled.")

    return build_fno_backtest_universe(engine_name, selection), summary_lines


def prompt_multi_strategy_selection(strategy_options):
    print("Choose strategies:")
    for key, value in strategy_options.items():
        print(f"{key}. {value}")
    print_prompt_help(
        "Enter one or more strategy numbers separated by commas.",
        "1,3,5",
    )
    raw = input("Enter comma-separated strategy numbers: ").strip()
    selected_keys = [item.strip() for item in raw.split(",") if item.strip()]
    if not selected_keys:
        selected_keys = [next(iter(strategy_options))]
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
            "Intraday options strategy: Momentum(1), ORB(2), VWAP Reversion(3), Multi-strategy(4), Breakout Expansion(5), IV Expansion(6), Trap Reversal(7) [default 1]: ",
            [
                {"key": 1, "value": "ATM_MOMENTUM"},
                {"key": 2, "value": "ATM_ORB"},
                {"key": 3, "value": "ATM_VWAP_REVERSION"},
                {"key": 4, "value": "ATM_MULTI"},
                {"key": 5, "value": "ATM_BREAKOUT_EXPANSION"},
                {"key": 6, "value": "ATM_IV_EXPANSION"},
                {"key": 7, "value": "ATM_TRAP_REVERSAL"},
            ],
            default=1,
        )
        return "SINGLE", strategy_name, (strategy_name,), 1

    if engine_class.name == "intraday_equity":
        print_prompt_help(
            "Choose whether to run one strategy, a combination, or auto-adaptive intraday equity mode.",
            "3 for Auto Adaptive",
        )
        mode = prompt_choice(
            "Strategy mode: Single(1), Multi(2), Auto Adaptive(3) [default 1]: ",
            [
                {"key": 1, "value": "SINGLE"},
                {"key": 2, "value": "MULTI"},
                {"key": 3, "value": "AUTO_ADAPTIVE"},
            ],
            default=1,
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
            "1 for Single",
        )
        mode = prompt_choice(
            "Strategy mode: Single(1) or Multi(2) [default 1]: ",
            [
                {"key": 1, "value": "SINGLE"},
                {"key": 2, "value": "MULTI"},
            ],
            default=1,
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
            default=1,
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
        "Trading engine: INTRADAY EQUITY(1), DELIVERY EQUITY(2), FUTURES EQUITY(3), OPTIONS EQUITY(4), INTRADAY FUTURES(5), INTRADAY OPTIONS(6) [default 1]: ",
        [
            {"key": 1, "value": "1"},
            {"key": 2, "value": "2"},
            {"key": 3, "value": "3"},
            {"key": 4, "value": "4"},
            {"key": 5, "value": "5"},
            {"key": 6, "value": "6"},
        ],
        default=1,
    )
    engine_class = ENGINE_OPTIONS[engine_choice]

    print_prompt_help(
        "Enter total capital available for this backtest run.",
        "100000",
    )
    capital = prompt_float("Enter capital for backtest: ", default=100000, minimum=1)

    if "futures" in engine_class.name or "options" in engine_class.name:
        universe, summary_lines = prompt_fno_contract_selection(engine_class.name)
    else:
        universe, summary_label = prompt_symbol_selection()
        summary_lines = [summary_label]

    print_prompt_help(
        "Choose the risk style that controls ATR stop, trailing stop, and capital risk.",
        "2 for BALANCED",
    )
    risk_style = RISK_STYLES[
        prompt_choice(
            "Risk style: CONSERVATIVE(1), BALANCED(2), AGGRESSIVE(3)? [default 2]: ",
            [
                {"key": 1, "value": "1"},
                {"key": 2, "value": "2"},
                {"key": 3, "value": "3"},
            ],
            default=2,
        )
    ]

    print_prompt_help(
        "Enter how many positions can be open at the same time.",
        "1",
    )
    max_positions = prompt_int("Max open positions [default 1]: ", default=1, minimum=1)
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
            "One trade per symbol per day? YES(1) or NO(2) [default 1]: ",
            [{"key": 1, "value": "YES"}, {"key": 2, "value": "NO"}],
            default=1,
        )
        == "YES"
    )

    print_prompt_help(
        "Choose whether to take only the top signal or multiple ranked signals.",
        "1 for TOP 1, 2 for TOP N",
    )
    entry_selection_mode = prompt_choice(
        "Entry selection: TOP 1(1) or TOP N(2)? [default 1]: ",
        [{"key": 1, "value": "TOP_1"}, {"key": 2, "value": "TOP_N"}],
        default=1,
    )
    top_n = 1
    if entry_selection_mode == "TOP_N":
        print_prompt_help(
            "Enter how many top-ranked signals should be entered each cycle.",
            "2",
        )
        top_n = prompt_int("How many top-ranked entries? [default 2]: ", default=2, minimum=1)

    strategy_mode, strategy_name, strategies, min_confirmations = prompt_strategy_setup(engine_class)

    default_period, default_interval = BACKTEST_DEFAULT_DATA.get(
        engine_class.name,
        (getattr(engine_class, "data_period", "6mo"), getattr(engine_class, "data_interval", "1d")),
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
        summary_lines.append(
            "Note: intraday options backtest uses the underlying spot index as a signal proxy. Expiry/structure inputs are captured for setup parity and reporting."
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
        top_n=max(1, top_n),
        max_positions=max_positions,
        max_capital_per_trade=max_capital_per_trade,
        max_capital_deployed=max_capital_deployed,
        universe=universe,
        one_trade_per_symbol_per_day=one_trade_per_symbol_per_day,
        summary_lines=summary_lines,
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
    config = summary["config"]
    print("\nBacktest summary")
    for line in config.summary_lines:
        print(f"- {line}")
    print(f"Ending equity: {summary['ending_equity']:.2f}")
    print(f"Total return: {summary['total_return_percent']:.2f}%")
    print(f"Closed trades: {summary['closed_trades']}")
    print(f"Win rate: {summary['win_rate_percent']:.2f}%")
    print(f"Max drawdown: {summary['max_drawdown_percent']:.2f}%")
    print(f"Estimated charges: {summary['total_estimated_charges']:.2f}")
    print(f"Estimated net P&L: {summary['total_net_pnl']:+.2f}")

    trades = summary["trades"]
    if not trades.empty:
        preview_columns = [
            "symbol",
            "side",
            "strategy",
            "option_signal",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "quantity",
            "pnl",
            "estimated_charges",
            "net_pnl",
            "exit_reason",
            "score",
        ]
        available_columns = [col for col in preview_columns if col in trades.columns]
        print("\nRecent trades")
        print(trades[available_columns].tail(10).to_string(index=False))

    summary_path, trades_path, equity_path = export_backtest_results(summary)
    print("\nHow to check results")
    print(f"- Summary: {summary_path}")
    print(f"- Trades CSV: {trades_path}")
    print(f"- Equity curve CSV: {equity_path}")
    print("- Open the trades CSV in Excel to inspect each entry, exit, P&L, and exit reason.")
    print("- Open the equity CSV to chart equity over time and review drawdowns.")


if __name__ == "__main__":
    summary = BacktestEngine(prompt_backtest_config()).run()
    print_summary(summary)
