from datetime import datetime, time

from config import ENGINE_DEFAULTS
from engines.common import build_position
from executor_fno import get_futures_positions
from fno_data_fetcher import get_contract_lot_size
from logger import log_event

from .futures_equity import FuturesEquityEngine


class IntradayFuturesEngine(FuturesEquityEngine):
    settings = ENGINE_DEFAULTS["intraday_futures"]
    name = "intraday_futures"
    data_period = str(settings.get("data_period", "15d"))
    data_interval = str(settings.get("data_interval", "3m"))
    order_product = "MIS"
    market_open = time(9, 15)
    entry_cutoff = datetime.strptime(str(settings.get("entry_cutoff", "15:05")), "%H:%M").time()
    square_off_time = datetime.strptime(str(settings.get("square_off_time", "15:15")), "%H:%M").time()
    market_close = time(15, 30)
    sleep_seconds = int(settings.get("sleep_seconds", 60))
    cooldown_seconds = int(settings.get("cooldown_seconds", 180))
    max_symbol_allocation = float(settings["max_symbol_allocation"])

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
                "reason": "Waiting for F&O market open at 09:15",
            }

        if current_time >= self.market_close:
            return {
                "manage_positions": False,
                "allow_entries": False,
                "force_square_off": False,
                "allow_scan": False,
                "reason": "Market closed for intraday futures trading",
            }

        if current_time >= self.square_off_time:
            return {
                "manage_positions": True,
                "allow_entries": False,
                "force_square_off": True,
                "allow_scan": False,
                "reason": "Intraday futures square-off window active",
            }

        if current_time >= self.entry_cutoff:
            return {
                "manage_positions": True,
                "allow_entries": False,
                "force_square_off": False,
                "allow_scan": True,
                "reason": "Intraday futures entry cutoff reached",
            }

        return {
            "manage_positions": True,
            "allow_entries": True,
            "force_square_off": False,
            "allow_scan": True,
            "reason": "Intraday futures session active",
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

    def reconcile_startup(self, execution_mode, persisted_positions):
        if execution_mode != "LIVE":
            log_event(
                f"[RECON] {self.name} running in paper mode - using persisted positions"
            )
            return persisted_positions

        try:
            broker_positions = {}
            for item in get_futures_positions(product="MIS"):
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

                broker_positions[symbol] = build_position(
                    symbol=symbol,
                    side="BUY" if quantity > 0 else "SELL",
                    quantity=abs(quantity),
                    entry_price=float(item.get("average_price") or 0),
                    sl_pct=self.sl_percent,
                    target_pct=self.target_percent,
                    trailing_pct=self.trailing_percent,
                    lot_size=get_contract_lot_size(symbol),
                )

            log_event(
                f"[RECON] Loaded {len(broker_positions)} live intraday futures positions from broker"
            )
            return broker_positions
        except NotImplementedError as ex:
            log_event(f"[RECON] Intraday futures startup sync unavailable: {ex}", "warning")
            return persisted_positions
