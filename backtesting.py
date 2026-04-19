import argparse
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

from config import NIFTY50_SYMBOLS
from engines.common import build_position, evaluate_exit, update_trailing_stop
from risk_manager import atr_position_size, atr_stop_from_value, calculate_target_price
from signal_scoring import evaluate_symbol_signal, get_atr_value, rank_candidates


DEFAULT_STRATEGIES = ["MA", "RSI", "BREAKOUT"]


@dataclass
class BacktestConfig:
    capital: float = 1_000_000.0
    period: str = "1y"
    interval: str = "1d"
    strategies: tuple = tuple(DEFAULT_STRATEGIES)
    min_confirmations: int = 2
    risk_percent: float = 0.01
    atr_stop_multiplier: float = 2.0
    trailing_atr_multiplier: float = 1.25
    target_risk_reward: float = 2.0
    top_n: int = 5
    max_positions: int = 5
    max_capital_per_trade: float | None = None
    universe: tuple = tuple(NIFTY50_SYMBOLS)
    long_only: bool = True


class BacktestEngine:
    def __init__(self, config):
        self.config = config
        self.cash = config.capital
        self.positions = {}
        self.trades = []
        self.equity_curve = []

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
            self._process_day(history, timestamp)

        self._close_all_open_positions(history, timeline[-1])
        return self._build_summary()

    def _process_day(self, history, timestamp):
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
                    self._exit_position(
                        symbol,
                        latest_prices[symbol],
                        timestamp,
                        exit_reason,
                    )
                continue

            evaluation = evaluate_symbol_signal(
                current_slice,
                mode="2",
                strategies=list(self.config.strategies),
                min_confirmations=self.config.min_confirmations,
            )
            if evaluation["signal"] not in {"BUY", "SELL"}:
                continue
            if self.config.long_only and evaluation["signal"] != "BUY":
                continue

            candidates.append(
                {
                    "symbol": symbol,
                    "signal": evaluation["signal"],
                    "agreement_count": evaluation["agreement_count"],
                    "score": evaluation["score"],
                    "latest_close": latest_prices[symbol],
                    "atr": get_atr_value(current_slice),
                }
            )

        ranked = rank_candidates(candidates)
        self._enter_ranked_candidates(ranked, timestamp)
        self._mark_equity(timestamp, latest_prices)

    def _enter_ranked_candidates(self, ranked_candidates, timestamp):
        if len(self.positions) >= self.config.max_positions:
            return

        for candidate in ranked_candidates[: self.config.top_n]:
            if len(self.positions) >= self.config.max_positions:
                break
            if candidate["symbol"] in self.positions:
                continue
            if candidate["atr"] <= 0:
                continue

            portfolio_equity = self.cash + sum(
                position["entry_price"] * position["quantity"]
                for position in self.positions.values()
            )
            sizing = atr_position_size(
                capital=portfolio_equity,
                entry_price=candidate["latest_close"],
                atr_value=candidate["atr"],
                atr_multiplier=self.config.atr_stop_multiplier,
                risk_percent=self.config.risk_percent,
            )
            qty = sizing["quantity"]
            if self.config.max_capital_per_trade:
                qty = min(
                    qty,
                    int(self.config.max_capital_per_trade / candidate["latest_close"]),
                )
            qty = min(qty, int(self.cash / candidate["latest_close"]))
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
            self.cash -= entry_price * qty
            self.trades.append(
                {
                    "symbol": candidate["symbol"],
                    "side": candidate["signal"],
                    "entry_time": timestamp,
                    "entry_price": entry_price,
                    "quantity": qty,
                    "score": candidate["score"],
                    "atr": candidate["atr"],
                }
            )

    def _exit_position(self, symbol, exit_price, timestamp, reason):
        position = self.positions.pop(symbol)
        quantity = position["quantity"]
        pnl = (
            (exit_price - position["entry_price"]) * quantity
            if position["side"] == "BUY"
            else (position["entry_price"] - exit_price) * quantity
        )
        self.cash += exit_price * quantity

        for trade in reversed(self.trades):
            if trade["symbol"] == symbol and "exit_time" not in trade:
                trade["exit_time"] = timestamp
                trade["exit_price"] = exit_price
                trade["exit_reason"] = reason
                trade["pnl"] = pnl
                break

    def _close_all_open_positions(self, history, timestamp):
        for symbol in list(self.positions):
            latest_df = history[symbol].loc[:timestamp]
            if latest_df.empty:
                continue
            exit_price = float(latest_df.iloc[-1]["Close"])
            self._exit_position(symbol, exit_price, timestamp, "END_OF_TEST")

    def _mark_equity(self, timestamp, latest_prices):
        market_value = 0.0
        for symbol, position in self.positions.items():
            price = latest_prices.get(symbol, position["entry_price"])
            market_value += price * position["quantity"]

        equity = self.cash + market_value
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

        return {
            "config": self.config,
            "ending_equity": ending_equity,
            "total_return_percent": total_return,
            "closed_trades": int(len(closed_trades)),
            "win_rate_percent": win_rate,
            "max_drawdown_percent": max_drawdown,
            "equity_curve": equity_df,
            "trades": trades_df,
        }


def print_summary(summary):
    print("Backtest summary")
    print(f"Ending equity: {summary['ending_equity']:.2f}")
    print(f"Total return: {summary['total_return_percent']:.2f}%")
    print(f"Closed trades: {summary['closed_trades']}")
    print(f"Win rate: {summary['win_rate_percent']:.2f}%")
    print(f"Max drawdown: {summary['max_drawdown_percent']:.2f}%")

    trades = summary["trades"]
    if not trades.empty:
        preview_columns = [
            "symbol",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "quantity",
            "pnl",
            "exit_reason",
            "score",
        ]
        available_columns = [col for col in preview_columns if col in trades.columns]
        print("\nRecent trades")
        print(trades[available_columns].tail(10).to_string(index=False))


def parse_args():
    parser = argparse.ArgumentParser(description="Daily-bar NIFTY50 backtester")
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("--period", default="1y")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--risk-percent", type=float, default=0.01)
    parser.add_argument("--atr-stop-multiplier", type=float, default=2.0)
    parser.add_argument("--trailing-atr-multiplier", type=float, default=1.25)
    parser.add_argument("--target-risk-reward", type=float, default=2.0)
    parser.add_argument(
        "--strategies",
        default="MA,RSI,BREAKOUT",
        help="Comma-separated strategies from MA,RSI,BREAKOUT,VWAP,ORB",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = BacktestConfig(
        capital=args.capital,
        period=args.period,
        strategies=tuple(
            strategy.strip().upper()
            for strategy in args.strategies.split(",")
            if strategy.strip()
        ),
        risk_percent=args.risk_percent,
        atr_stop_multiplier=args.atr_stop_multiplier,
        trailing_atr_multiplier=args.trailing_atr_multiplier,
        target_risk_reward=args.target_risk_reward,
        top_n=max(1, args.top_n),
        max_positions=max(1, args.max_positions),
        max_capital_per_trade=args.capital / max(1, args.max_positions),
    )
    summary = BacktestEngine(config).run()
    print_summary(summary)
