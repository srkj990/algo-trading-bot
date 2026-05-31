"""
KiteConnect WebSocket tick manager.

Runs KiteTicker in a background thread and maintains the latest LTP per
instrument token. Callers subscribe by instrument token (integer) and read
`get_ltp(token)` at any time without blocking.

Usage
-----
    ticker = KiteTickerManager(api_key, access_token)
    ticker.start()
    ticker.subscribe([738561, 5633])          # RELIANCE, ACC tokens
    ltp = ticker.get_ltp(738561)
    ticker.stop()
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


class KiteTickerManager:
    """Thread-safe WebSocket tick aggregator using KiteTicker."""

    def __init__(self, api_key: str, access_token: str) -> None:
        self._api_key = api_key
        self._access_token = access_token
        self._kws = None
        self._thread: threading.Thread | None = None
        self._ltp: dict[int, float] = {}
        self._lock = threading.Lock()
        self._running = False
        self._connected = threading.Event()
        # token -> list of (trigger_level, direction, callback, fired_flag)
        self._arms: dict[int, list[dict]] = {}
        # token -> callback invoked on every tick (for trailing ratchet)
        self._continuous: dict[int, Callable[[int, float], None]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="KiteTicker")
        self._thread.start()
        connected = self._connected.wait(timeout=15)
        if not connected:
            logger.warning("[TICKER] WebSocket did not connect within 15s — running without live ticks")

    def stop(self) -> None:
        self._running = False
        if self._kws:
            try:
                self._kws.stop()
            except Exception:
                pass
        self._connected.clear()

    def subscribe(self, tokens: list[int]) -> None:
        if not self._kws or not self._connected.is_set():
            return
        try:
            from kiteconnect import KiteTicker  # noqa: F401
            self._kws.subscribe(tokens)
            self._kws.set_mode(self._kws.MODE_LTP, tokens)
        except Exception as exc:
            logger.warning(f"[TICKER] Subscribe failed: {exc}")

    def unsubscribe(self, tokens: list[int]) -> None:
        if not self._kws or not self._connected.is_set():
            return
        try:
            self._kws.unsubscribe(tokens)
        except Exception as exc:
            logger.warning(f"[TICKER] Unsubscribe failed: {exc}")

    def get_ltp(self, token: int) -> float | None:
        with self._lock:
            return self._ltp.get(token)

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def arm_breakout(
        self,
        token: int,
        trigger_price: float,
        direction: str,
        callback: Callable[[int, float], None],
    ) -> None:
        """
        Fire `callback(token, ltp)` once when ltp crosses trigger_price.

        direction: "BUY" fires when ltp >= trigger_price
                   "SELL" fires when ltp <= trigger_price
        """
        arm = {"trigger": trigger_price, "direction": direction, "callback": callback, "fired": False}
        with self._lock:
            self._arms.setdefault(token, []).append(arm)

    def disarm_all(self, token: int) -> None:
        with self._lock:
            self._arms.pop(token, None)

    def arm_continuous(self, token: int, callback: Callable[[int, float], None]) -> None:
        """Register a callback called on every tick for this token (for trailing ratchet)."""
        with self._lock:
            self._continuous[token] = callback

    def disarm_continuous(self, token: int) -> None:
        with self._lock:
            self._continuous.pop(token, None)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            from kiteconnect import KiteTicker
        except ImportError:
            logger.error("[TICKER] kiteconnect package not available — WebSocket disabled")
            return

        kws = KiteTicker(self._api_key, self._access_token)
        self._kws = kws

        def on_connect(ws, response):
            self._connected.set()
            logger.info("[TICKER] WebSocket connected")

        def on_ticks(ws, ticks):
            with self._lock:
                for tick in ticks:
                    token = int(tick.get("instrument_token", 0))
                    ltp = float(tick.get("last_price") or tick.get("ltp") or 0.0)
                    if token and ltp:
                        self._ltp[token] = ltp
                        self._check_arms(token, ltp)

        def on_close(ws, code, reason):
            self._connected.clear()
            logger.warning(f"[TICKER] WebSocket closed: {code} {reason}")

        def on_error(ws, code, reason):
            logger.error(f"[TICKER] WebSocket error: {code} {reason}")

        def on_reconnect(ws, attempts_count):
            logger.info(f"[TICKER] Reconnecting (attempt {attempts_count})")

        def on_noreconnect(ws):
            logger.error("[TICKER] WebSocket max reconnect attempts reached")
            self._running = False

        kws.on_connect = on_connect
        kws.on_ticks = on_ticks
        kws.on_close = on_close
        kws.on_error = on_error
        kws.on_reconnect = on_reconnect
        kws.on_noreconnect = on_noreconnect

        try:
            kws.connect(threaded=False)
        except Exception as exc:
            logger.error(f"[TICKER] WebSocket connect error: {exc}")
        finally:
            self._connected.clear()

    def _check_arms(self, token: int, ltp: float) -> None:
        """Called inside lock — fire any armed breakouts and continuous callbacks."""
        arms = self._arms.get(token, [])
        for arm in arms:
            if arm["fired"]:
                continue
            direction = arm["direction"]
            trigger = arm["trigger"]
            triggered = (direction == "BUY" and ltp >= trigger) or (direction == "SELL" and ltp <= trigger)
            if triggered:
                arm["fired"] = True
                cb = arm["callback"]
                t = threading.Thread(target=cb, args=(token, ltp), daemon=True)
                t.start()

        cont_cb = self._continuous.get(token)
        if cont_cb:
            t = threading.Thread(target=cont_cb, args=(token, ltp), daemon=True)
            t.start()


# ---------------------------------------------------------------------------
# Singleton helpers – the session holds one manager for the process lifetime
# ---------------------------------------------------------------------------

_manager: KiteTickerManager | None = None
_manager_lock = threading.Lock()


def get_ticker_manager(api_key: str | None = None, access_token: str | None = None) -> KiteTickerManager | None:
    """
    Return the process-wide KiteTickerManager, creating it if api_key/access_token are
    supplied and it hasn't been started yet.  Returns None if credentials are absent.
    """
    global _manager
    with _manager_lock:
        if _manager is None:
            if not api_key or not access_token:
                return None
            _manager = KiteTickerManager(api_key, access_token)
            _manager.start()
        return _manager


def shutdown_ticker() -> None:
    global _manager
    with _manager_lock:
        if _manager:
            _manager.stop()
            _manager = None


def wait_for_ltp(
    get_ltp_fn: Callable[[], float | None],
    trigger_price: float,
    direction: str,
    poll_interval: float = 8.0,
    max_wait: float = 55.0,
) -> float | None:
    """
    Busy-poll fallback (REST LTP) when WebSocket is unavailable.
    Returns the live price that crossed the trigger, or None on timeout.
    """
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        ltp = get_ltp_fn()
        if ltp:
            crossed = (direction == "BUY" and ltp >= trigger_price) or (
                direction == "SELL" and ltp <= trigger_price
            )
            if crossed:
                return ltp
        time.sleep(poll_interval)
    return None
