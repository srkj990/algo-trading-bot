"""
Lightweight transaction cost estimation utilities.

This is intentionally "good enough" for pre-trade filtering and paper-mode realism.
Rates are approximate and should be calibrated for your broker/account.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostBreakdown:
    turnover: float
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    stamp: float
    gst: float
    slippage: float

    @property
    def total(self) -> float:
        return (
            self.brokerage
            + self.stt
            + self.exchange_txn
            + self.sebi
            + self.stamp
            + self.gst
            + self.slippage
        )


def estimate_intraday_equity_round_trip_cost(
    *,
    entry_side: str = "BUY",
    entry_price: float,
    exit_price: float,
    quantity: int,
    slippage_pct_per_side: float = 0.0,
    # Zerodha-like defaults (intraday equity).
    brokerage_rate_per_side: float = 0.0003,  # 0.03%
    brokerage_cap_per_order: float = 20.0,
    stt_sell_rate: float = 0.00025,  # 0.025% on sell side
    exchange_txn_rate_per_side: float = 0.0000345,  # NSE: ~0.00345%
    sebi_rate_per_side: float = 0.000001,  # 0.0001%
    stamp_buy_rate: float = 0.00003,  # 0.003% on buy side (intraday equity)
    gst_rate: float = 0.18,  # on brokerage + exchange + sebi
) -> CostBreakdown:
    """
    Approx intraday equity round-trip costs in INR for one buy and one sell.

    We assume one buy and one sell (regardless of long/short), and apply:
    - brokerage each side (capped)
    - STT on sell
    - exchange txn each side
    - SEBI each side
    - stamp on buy
    - GST on (brokerage + exchange + sebi)
    - slippage each side (simple pct of price)
    """

    qty = int(quantity or 0)
    if qty <= 0 or entry_price <= 0 or exit_price <= 0:
        return CostBreakdown(
            turnover=0.0,
            brokerage=0.0,
            stt=0.0,
            exchange_txn=0.0,
            sebi=0.0,
            stamp=0.0,
            gst=0.0,
            slippage=0.0,
        )

    entry_side = (entry_side or "BUY").upper()
    if entry_side == "SELL":
        sell_value = entry_price * qty
        buy_value = exit_price * qty
    else:
        buy_value = entry_price * qty
        sell_value = exit_price * qty

    # Use mid turnover as a stable approximation for each leg.
    trade_value = ((entry_price + exit_price) / 2.0) * qty
    turnover = trade_value * 2.0

    brokerage_per_side = min(brokerage_cap_per_order, trade_value * brokerage_rate_per_side)
    brokerage = brokerage_per_side * 2.0

    exchange_txn = trade_value * exchange_txn_rate_per_side * 2.0
    sebi = trade_value * sebi_rate_per_side * 2.0
    stt = sell_value * stt_sell_rate
    stamp = buy_value * stamp_buy_rate

    gst_base = brokerage + exchange_txn + sebi
    gst = gst_rate * gst_base

    slippage = (trade_value * slippage_pct_per_side) * 2.0

    return CostBreakdown(
        turnover=turnover,
        brokerage=brokerage,
        stt=stt,
        exchange_txn=exchange_txn,
        sebi=sebi,
        stamp=stamp,
        gst=gst,
        slippage=slippage,
    )


def estimate_delivery_equity_round_trip_cost(
    *,
    entry_side: str = "BUY",
    entry_price: float,
    exit_price: float,
    quantity: int,
    slippage_pct_per_side: float = 0.0,
    brokerage_rate_per_side: float = 0.0,
    brokerage_cap_per_order: float = 20.0,
    stt_sell_rate: float = 0.001,  # 0.1% on sell side
    exchange_txn_rate_per_side: float = 0.0000345,
    sebi_rate_per_side: float = 0.000001,
    stamp_buy_rate: float = 0.00015,  # 0.015% on buy side
    gst_rate: float = 0.18,
) -> CostBreakdown:
    return _estimate_round_trip_cost(
        entry_side=entry_side,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        slippage_pct_per_side=slippage_pct_per_side,
        brokerage_rate_per_side=brokerage_rate_per_side,
        brokerage_cap_per_order=brokerage_cap_per_order,
        stt_sell_rate=stt_sell_rate,
        exchange_txn_rate_per_side=exchange_txn_rate_per_side,
        sebi_rate_per_side=sebi_rate_per_side,
        stamp_buy_rate=stamp_buy_rate,
        gst_rate=gst_rate,
    )


def estimate_futures_round_trip_cost(
    *,
    entry_side: str = "BUY",
    entry_price: float,
    exit_price: float,
    quantity: int,
    slippage_pct_per_side: float = 0.0,
    brokerage_rate_per_side: float = 0.0003,
    brokerage_cap_per_order: float = 20.0,
    stt_sell_rate: float = 0.0002,  # approx futures sell-side STT
    exchange_txn_rate_per_side: float = 0.00002,
    sebi_rate_per_side: float = 0.000001,
    stamp_buy_rate: float = 0.00002,
    gst_rate: float = 0.18,
) -> CostBreakdown:
    return _estimate_round_trip_cost(
        entry_side=entry_side,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        slippage_pct_per_side=slippage_pct_per_side,
        brokerage_rate_per_side=brokerage_rate_per_side,
        brokerage_cap_per_order=brokerage_cap_per_order,
        stt_sell_rate=stt_sell_rate,
        exchange_txn_rate_per_side=exchange_txn_rate_per_side,
        sebi_rate_per_side=sebi_rate_per_side,
        stamp_buy_rate=stamp_buy_rate,
        gst_rate=gst_rate,
    )


def estimate_options_round_trip_cost(
    *,
    entry_side: str = "BUY",
    entry_price: float,
    exit_price: float,
    quantity: int,
    slippage_pct_per_side: float = 0.0,
    brokerage_rate_per_side: float = 0.0003,
    brokerage_cap_per_order: float = 20.0,
    stt_sell_rate: float = 0.0005,  # approx options sell-side premium STT
    exchange_txn_rate_per_side: float = 0.00053,
    sebi_rate_per_side: float = 0.000001,
    stamp_buy_rate: float = 0.00003,
    gst_rate: float = 0.18,
) -> CostBreakdown:
    return _estimate_round_trip_cost(
        entry_side=entry_side,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        slippage_pct_per_side=slippage_pct_per_side,
        brokerage_rate_per_side=brokerage_rate_per_side,
        brokerage_cap_per_order=brokerage_cap_per_order,
        stt_sell_rate=stt_sell_rate,
        exchange_txn_rate_per_side=exchange_txn_rate_per_side,
        sebi_rate_per_side=sebi_rate_per_side,
        stamp_buy_rate=stamp_buy_rate,
        gst_rate=gst_rate,
    )


def _estimate_round_trip_cost(
    *,
    entry_side: str,
    entry_price: float,
    exit_price: float,
    quantity: int,
    slippage_pct_per_side: float,
    brokerage_rate_per_side: float,
    brokerage_cap_per_order: float,
    stt_sell_rate: float,
    exchange_txn_rate_per_side: float,
    sebi_rate_per_side: float,
    stamp_buy_rate: float,
    gst_rate: float,
) -> CostBreakdown:
    qty = int(quantity or 0)
    if qty <= 0 or entry_price <= 0 or exit_price <= 0:
        return CostBreakdown(
            turnover=0.0,
            brokerage=0.0,
            stt=0.0,
            exchange_txn=0.0,
            sebi=0.0,
            stamp=0.0,
            gst=0.0,
            slippage=0.0,
        )

    entry_side = (entry_side or "BUY").upper()
    if entry_side == "SELL":
        sell_value = entry_price * qty
        buy_value = exit_price * qty
    else:
        buy_value = entry_price * qty
        sell_value = exit_price * qty

    entry_value = entry_price * qty
    exit_value = exit_price * qty
    turnover = entry_value + exit_value
    average_leg_value = turnover / 2.0

    brokerage_per_side = min(
        brokerage_cap_per_order,
        average_leg_value * brokerage_rate_per_side,
    )
    brokerage = brokerage_per_side * 2.0
    exchange_txn = average_leg_value * exchange_txn_rate_per_side * 2.0
    sebi = average_leg_value * sebi_rate_per_side * 2.0
    stt = sell_value * stt_sell_rate
    stamp = buy_value * stamp_buy_rate
    gst_base = brokerage + exchange_txn + sebi
    gst = gst_rate * gst_base
    slippage = average_leg_value * slippage_pct_per_side * 2.0

    return CostBreakdown(
        turnover=turnover,
        brokerage=brokerage,
        stt=stt,
        exchange_txn=exchange_txn,
        sebi=sebi,
        stamp=stamp,
        gst=gst,
        slippage=slippage,
    )
