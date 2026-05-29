"""Centralized terminal display utilities shared across all engines and backtest."""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from typing import Any

_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_GREEN = "\033[92m"
_RED   = "\033[91m"
_YELLOW = "\033[93m"
_CYAN  = "\033[96m"
_RESET = "\033[0m"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _use_color() -> bool:
    return (
        sys.stdout.isatty()
        and os.environ.get("NO_COLOR") is None
        and os.environ.get("TERM") != "dumb"
    )


def _c(text: str, code: str) -> str:
    if _use_color():
        return f"{code}{text}{_RESET}"
    return text


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (used before writing to log files)."""
    return _ANSI_RE.sub("", text)


def _cf(text: str, code: str, width: int = 0, align: str = "<") -> str:
    """Pad to width FIRST, then colorize — keeps column alignment intact."""
    padded = f"{text:{align}{width}}" if width else text
    return _c(padded, code)


# ---------------------------------------------------------------------------
# Per-value formatters
# ---------------------------------------------------------------------------

def fmt_pnl(pnl_abs: float, pnl_pct: float | None = None) -> str:
    pct = f" ({pnl_pct:+.2f}%)" if pnl_pct is not None else ""
    return _c(f"{pnl_abs:+.2f}{pct}", _GREEN if pnl_abs >= 0 else _RED)


def fmt_signal(signal: str, width: int = 0) -> str:
    return _cf(signal, _GREEN if signal == "BUY" else _RED, width)


def fmt_side(side: str, width: int = 0) -> str:
    return _cf(side, _GREEN if side == "BUY" else _RED, width)


def fmt_header(text: str) -> str:
    return _c(text, _BOLD + _CYAN)


def fmt_warn(text: str) -> str:
    return _c(text, _YELLOW)


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def banner(
    log_event,
    title: str,
    lines: list[str],
    prefix: str = "ORDER",
    width: int = 72,
) -> None:
    """Bordered banner used for entry/exit events."""
    border = "=" * width
    log_event(border)
    log_event(f"[{prefix}] {_c(title, _BOLD)}")
    for line in lines:
        log_event(f"[{prefix}] {line}")
    log_event(border)


def cycle_banner(log_event, label: str, now: datetime | None = None) -> None:
    """Compact timestamped banner printed at the start of each trading cycle."""
    ts = (now or datetime.now()).strftime("%H:%M:%S")
    width = 52
    border = "-" * width
    log_event(f"\n{border}")
    log_event(fmt_header(f"  Cycle {ts}  {label}"))
    log_event(border)


def config_summary(
    log_event,
    rows: list[tuple[str, str]],
    title: str = "Session Configuration",
) -> None:
    """Print a clean aligned two-column configuration table."""
    if not rows:
        return
    label_w = max(len(r[0]) for r in rows) + 2
    width = max(label_w + 36, len(title) + 6)
    border = "=" * width
    log_event(f"\n{border}")
    log_event(fmt_header(f"  {title}"))
    log_event(border)
    for label, value in rows:
        log_event(f"  {label:<{label_w}}{value}")
    log_event(f"{border}\n")


# ---------------------------------------------------------------------------
# Table displays
# ---------------------------------------------------------------------------

def positions_table(
    log_event,
    positions: dict[str, Any],
    current_prices: dict[str, float] | None = None,
    prefix: str = "POSITION",
) -> None:
    """Print open positions as an aligned, color-coded table."""
    if not positions:
        log_event(f"[{prefix}] Flat — no open positions")
        return

    rows = []
    for symbol, pos in positions.items():
        current = (current_prices or {}).get(symbol, pos["best_price"])
        if pos["side"] == "BUY":
            pnl = (current - pos["entry_price"]) * pos["quantity"]
        else:
            pnl = (pos["entry_price"] - current) * pos["quantity"]
        pnl_pct = (pnl / (pos["entry_price"] * pos["quantity"])) * 100 if pos["entry_price"] > 0 else 0.0
        rows.append((symbol, pos, current, pnl, pnl_pct))

    sym_w = max(max(len(r[0]) for r in rows), 6)
    log_event(
        f"[{prefix}]  {'Symbol':<{sym_w}}  {'Side':<4}  {'Qty':>5}  "
        f"{'Entry':>8}  {'Now':>8}  {'SL':>8}  {'Target':>8}  {'Trail':>8}  P&L"
    )
    log_event(f"[{prefix}]  {'-' * (sym_w + 67)}")
    for symbol, pos, current, pnl, pnl_pct in rows:
        side_str = _cf(pos["side"], _GREEN if pos["side"] == "BUY" else _RED, 4)
        pnl_color = _GREEN if pnl >= 0 else _RED
        pnl_str = _c(f"{pnl:+.2f} ({pnl_pct:+.2f}%)", pnl_color)
        log_event(
            f"[{prefix}]  {symbol:<{sym_w}}  {side_str}  {pos['quantity']:>5}  "
            f"{pos['entry_price']:>8.2f}  {current:>8.2f}  "
            f"{pos['stop_loss']:>8.2f}  {pos['target']:>8.2f}  "
            f"{pos['trailing_stop']:>8.2f}  {pnl_str}"
        )


def ranked_candidates_table(
    log_event,
    candidates: list[dict[str, Any]],
) -> None:
    """Print scan candidates as an aligned table with colored signals."""
    if not candidates:
        log_event("[SCAN] No actionable ranked candidates")
        return

    sym_w = max(max(len(c["symbol"]) for c in candidates), 6)
    log_event(
        f"[SCAN]  {'#':>2}  {'Symbol':<{sym_w}}  {'Sig':<4}  "
        f"{'Agree':>5}  {'Score':>7}  {'ATR':>7}  {'Close':>8}  Info"
    )
    log_event(f"[SCAN]  {'-' * (sym_w + 57)}")
    for i, c in enumerate(candidates, 1):
        analytics = c.get("analytics") or {}
        sig_str = _cf(c["signal"], _GREEN if c["signal"] == "BUY" else _RED, 4)
        if c.get("is_pair"):
            pair_data = c.get("pair_config") or {}
            info = (
                f"Pair {pair_data.get('lower_strike')}-{pair_data.get('upper_strike')}  "
                f"Under={analytics.get('underlying_price', 0.0):.2f}"
            )
        elif analytics:
            info = f"Delta={analytics.get('delta', 0.0):.3f}  IV={analytics.get('iv', 0.0):.3f}"
        else:
            info = ""
        log_event(
            f"[SCAN]  {i:>2}  {c['symbol']:<{sym_w}}  {sig_str}  "
            f"{c['agreement_count']:>5}  {c['score']:>7.4f}  "
            f"{c.get('atr', 0.0):>7.2f}  {c['latest_close']:>8.2f}  {info}"
        )


def trade_book_table(
    log_event,
    trades: list[dict[str, Any]],
    capital: float,
    transaction_cost_model_enabled: bool = True,
) -> None:
    """Print closed trades with an aligned table, colored P&L, and summary stats."""
    if not trades:
        log_event("[REPORT] No closed trades recorded in this session")
        return

    sorted_trades = sorted(trades, key=lambda t: t.get("exit_time") or "")
    wins   = sum(1 for t in sorted_trades if t["pnl"] > 0)
    losses = sum(1 for t in sorted_trades if t["pnl"] < 0)
    flats  = len(sorted_trades) - wins - losses
    total_gross   = sum(t["pnl"] for t in sorted_trades)
    total_net     = sum(float(t.get("net_pnl") or t["pnl"]) for t in sorted_trades)
    total_charges = sum(float(t.get("estimated_charges") or 0.0) for t in sorted_trades)
    win_rate = (wins / len(sorted_trades)) * 100 if sorted_trades else 0.0
    best  = max(sorted_trades, key=lambda t: t["pnl"])
    worst = min(sorted_trades, key=lambda t: t["pnl"])

    gross_str = _c(f"{total_gross:+.2f}", _GREEN if total_gross >= 0 else _RED)
    net_str   = _c(f"{total_net:+.2f}",   _GREEN if total_net   >= 0 else _RED)
    ret_pct   = (total_gross / capital) * 100 if capital > 0 else 0.0
    ret_str   = _c(f"{ret_pct:+.2f}%", _GREEN if ret_pct >= 0 else _RED)

    log_event(
        f"[REPORT] Closed={len(sorted_trades)}  Wins={wins}  Losses={losses}  "
        f"Flat={flats}  WinRate={win_rate:.1f}%"
    )
    if transaction_cost_model_enabled:
        log_event(f"[REPORT] Gross {gross_str}  Charges={total_charges:.2f}  Net {net_str}")
    else:
        log_event(f"[REPORT] Gross P&L {gross_str}")
    log_event(f"[REPORT] Return on capital {ret_str}")

    best_pnl_str  = _c(f"{best['pnl']:+.2f}",  _GREEN if best['pnl']  >= 0 else _RED)
    worst_pnl_str = _c(f"{worst['pnl']:+.2f}", _GREEN if worst['pnl'] >= 0 else _RED)
    log_event(f"[REPORT] Best   {best['symbol']}  {best_pnl_str}")
    log_event(f"[REPORT] Worst  {worst['symbol']}  {worst_pnl_str}")

    sym_w = max(max(len(t["symbol"]) for t in sorted_trades), 6)
    log_event(
        f"\n[REPORT]  {'#':>2}  {'Symbol':<{sym_w}}  {'Side':<4}  {'Qty':>4}  "
        f"{'Entry':>8}  {'Exit':>8}  {'P&L':<18}  Reason"
    )
    log_event(f"[REPORT]  {'-' * (sym_w + 64)}")
    for i, t in enumerate(sorted_trades, 1):
        pair_sfx  = f"[{t['pair_id']}]" if t.get("pair_id") else ""
        sym_disp  = f"{t['symbol']}{pair_sfx}"
        side_str  = _cf(t["side"], _GREEN if t["side"] == "BUY" else _RED, 4)
        pnl_color = _GREEN if t["pnl"] >= 0 else _RED
        pnl_str   = _c(f"{t['pnl']:+.2f} ({t.get('pnl_pct', 0.0):+.2f}%)", pnl_color)
        log_event(
            f"[REPORT]  {i:>2}  {sym_disp:<{sym_w}}  {side_str}  {t['quantity']:>4}  "
            f"{t['entry_price']:>8.2f}  {t['exit_price']:>8.2f}  {pnl_str:<18}  {t['exit_reason']}"
        )

    # Exit-reason summary
    from collections import defaultdict
    reason_buckets: dict[str, dict] = defaultdict(lambda: {"trades": 0, "gross": 0.0, "net": 0.0, "wins": 0})
    for t in sorted_trades:
        r = t.get("exit_reason", "UNKNOWN")
        reason_buckets[r]["trades"] += 1
        reason_buckets[r]["gross"]  += t["pnl"]
        reason_buckets[r]["net"]    += float(t.get("net_pnl") or t["pnl"])
        if t["pnl"] > 0:
            reason_buckets[r]["wins"] += 1

    log_event("\n[REPORT] Exit-reason breakdown:")
    for reason, b in sorted(reason_buckets.items()):
        wr = (b["wins"] / b["trades"]) * 100 if b["trades"] else 0.0
        g_str = _c(f"{b['gross']:+.2f}", _GREEN if b["gross"] >= 0 else _RED)
        n_str = _c(f"{b['net']:+.2f}",   _GREEN if b["net"]   >= 0 else _RED)
        log_event(
            f"[REPORT]   {reason:<20} Trades={b['trades']:>3}  "
            f"Gross={g_str}  Net={n_str}  WR={wr:.0f}%"
        )


def backtest_summary(summary: dict[str, Any]) -> None:
    """Print formatted backtest results with colors and aligned layout."""
    config = summary["config"]
    width = 58
    border = "=" * width

    print(f"\n{border}")
    print(fmt_header("  Backtest Summary"))
    print(border)
    for line in config.summary_lines:
        print(f"  {line}")
    print(border)

    ret_pct = summary["total_return_percent"]
    net_pnl = summary["total_net_pnl"]
    dd_pct  = summary["max_drawdown_percent"]

    ret_str = _c(f"{ret_pct:+.2f}%", _GREEN if ret_pct >= 0 else _RED)
    net_str = _c(f"{net_pnl:+.2f}",  _GREEN if net_pnl  >= 0 else _RED)
    dd_str  = _c(f"{dd_pct:.2f}%",   _RED if dd_pct > 5 else _YELLOW if dd_pct > 2 else _GREEN)

    rows = [
        ("Ending equity",  f"{summary['ending_equity']:.2f}"),
        ("Total return",   ret_str),
        ("Closed trades",  str(summary["closed_trades"])),
        ("Win rate",       f"{summary['win_rate_percent']:.2f}%"),
        ("Max drawdown",   dd_str),
        ("Est. charges",   f"{summary['total_estimated_charges']:.2f}"),
        ("Est. net P&L",   net_str),
    ]
    if summary.get("tick_entry_fills", 0) > 0:
        rows.append(("Tick-entry fills", str(summary["tick_entry_fills"])))

    label_w = max(len(r[0]) for r in rows) + 2
    for label, value in rows:
        print(f"  {label:<{label_w}}{value}")
    print(border)

    trades = summary.get("trades")
    import pandas as pd
    if trades is not None and not trades.empty:
        preview_cols = [
            "symbol", "side", "strategy", "option_signal",
            "entry_time", "exit_time", "entry_price", "exit_price",
            "quantity", "pnl", "estimated_charges", "net_pnl",
            "exit_reason", "score",
        ]
        available = [c for c in preview_cols if c in trades.columns]
        print(f"\n{fmt_header('  Recent trades (last 10)')}")
        print(trades[available].tail(10).to_string(index=False))
