"""
FormingCandlePreview — sub-1m signal evaluation using live WebSocket LTP.

Synthesises a partial (forming) candle from the current LTP and appends it
to the closed-candle history, then runs the engine's signal scanner with
``require_closed_signal_candle=False``.  Returns candidates flagged as
``signal_source="FORMING_TICK"`` if the LTP has crossed the ORB/momentum
threshold AND held there for at least ``confirm_ticks`` consecutive ticks.

Activation:
    - engine is ``intraday_options``
    - ``forming_tick_enabled=True`` in session config
    - KiteTickerManager is connected

Only for LIVE / PAPER sessions — not used in backtest (no WebSocket).
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from typing import Any

import pandas as pd

from config import is_intraday_options_engine_name

logger = logging.getLogger(__name__)


class FormingCandlePreview:
    """
    One instance per session.  Call ``scan()`` after each closed-candle scan
    to get additional FORMING_TICK candidates.
    """

    def __init__(self, context: Any, confirm_ticks: int = 2) -> None:
        self._ctx = context
        self._confirm_ticks = max(1, int(confirm_ticks))
        self._lock = threading.Lock()
        # underlying -> deque of (ltp, above_threshold: bool)
        self._tick_history: dict[str, list[tuple[float, bool]]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self, closed_history: dict[str, pd.DataFrame], ltp_map: dict[str, float]) -> list[dict]:
        """
        Run a forming-candle signal scan.

        :param closed_history: symbol -> closed-candle DataFrame (from session history).
        :param ltp_map: symbol -> current live LTP (option or underlying).
        :returns: list of candidate dicts with signal_source="FORMING_TICK".
        """
        candidates = []
        engine = getattr(self._ctx, "engine", None)
        if engine is None or not is_intraday_options_engine_name(getattr(engine, "name", "")):
            return candidates

        scanner = getattr(engine, "scan_signals", None)
        if not callable(scanner):
            return candidates

        for symbol, history_df in closed_history.items():
            if history_df is None or history_df.empty:
                continue
            ltp = ltp_map.get(symbol)
            if not ltp or ltp <= 0:
                continue

            forming_df = self._build_forming_df(history_df, ltp)
            if forming_df is None:
                continue

            try:
                raw_candidates = scanner(
                    symbol,
                    forming_df,
                    require_closed_signal_candle=False,
                )
            except Exception:
                logger.debug("FormingCandlePreview scan error for %s", symbol, exc_info=True)
                continue

            for cand in raw_candidates or []:
                above = self._is_above_threshold(cand, ltp)
                confirmed = self._update_tick_history(symbol, ltp, above)
                if confirmed:
                    enriched = dict(cand)
                    enriched["signal_source"] = "FORMING_TICK"
                    enriched["forming_ltp"] = ltp
                    candidates.append(enriched)

        return candidates

    def reset_symbol(self, symbol: str) -> None:
        """Clear tick history for a symbol (call on new day or after entry)."""
        with self._lock:
            self._tick_history.pop(symbol, None)

    def reset_all(self) -> None:
        with self._lock:
            self._tick_history.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_forming_df(history_df: pd.DataFrame, ltp: float) -> pd.DataFrame | None:
        """Append a synthetic partial candle representing the current forming 1m bar."""
        try:
            last_close = float(history_df["Close"].iloc[-1])
            forming_row = {
                "Open":   last_close,
                "High":   max(last_close, ltp),
                "Low":    min(last_close, ltp),
                "Close":  ltp,
                "Volume": 0,
            }
            # Use a synthetic timestamp 1 minute after the last closed candle
            last_ts = history_df.index[-1]
            if isinstance(last_ts, pd.Timestamp):
                forming_ts = last_ts + pd.Timedelta(minutes=1)
            else:
                forming_ts = pd.Timestamp(last_ts) + pd.Timedelta(minutes=1)
            forming_series = pd.Series(forming_row, name=forming_ts)
            return pd.concat([history_df, forming_series.to_frame().T])
        except Exception:
            return None

    @staticmethod
    def _is_above_threshold(candidate: dict, ltp: float) -> bool:
        """Return True if the candidate's signal direction matches the LTP position."""
        signal = str(candidate.get("signal") or candidate.get("option_signal") or "").upper()
        orb_high = float(candidate.get("orb_high") or 0)
        orb_low = float(candidate.get("orb_low") or 0)
        if signal in {"BUY", "BUY_CE"}:
            return orb_high <= 0 or ltp > orb_high
        if signal in {"SELL", "BUY_PE"}:
            return orb_low <= 0 or ltp < orb_low
        return True

    def _update_tick_history(self, symbol: str, ltp: float, above: bool) -> bool:
        """
        Record tick.  Return True if LTP has been consistently above/below
        threshold for at least ``confirm_ticks`` consecutive ticks.
        """
        with self._lock:
            hist = self._tick_history[symbol]
            hist.append((ltp, above))
            # Keep only the last confirm_ticks entries
            if len(hist) > self._confirm_ticks:
                self._tick_history[symbol] = hist[-self._confirm_ticks:]
                hist = self._tick_history[symbol]
            if len(hist) < self._confirm_ticks:
                return False
            return all(a for _, a in hist)
