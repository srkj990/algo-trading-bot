"""
Live Warning Center — computes structured advisory warnings + market intelligence.

Called once per cycle after scan_symbols() completes. Returns:
  - warnings     : list of Warning dicts (active this cycle)
  - market_intel : rich market-level summary for the UI intelligence panel

Warning schema:
    {
      "id":        str,    # stable ID for deduplication
      "severity":  "critical"|"warning"|"info",
      "category":  str,    # VOLATILITY|VWAP|TREND|TIME|RISK|OPTIONS|REGIME|INFRA|SIGNAL
      "title":     str,    # short headline (< 60 chars)
      "detail":    str,    # human-readable explanation
      "symbol":    str|None,
      "ts":        str,    # HH:MM:SS
    }
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any

import pandas as pd

# Ring buffer of all emitted warnings. max 200 entries.
_warning_history: deque[dict[str, Any]] = deque(maxlen=200)
_active_ids: set[str] = set()
_last_cycle_ids: set[str] = set()


# ── public API ────────────────────────────────────────────────────────────────

def compute_warnings(
    context: Any,
    symbol_snapshots: dict[str, Any],
    now: datetime,
    vix: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Main entry point. Called after scan_symbols() each cycle.
    Returns (warnings, market_intel).
    """
    ts = now.strftime("%H:%M:%S")
    warnings: list[dict[str, Any]] = []

    # ── 1. Volatility guards ─────────────────────────────────────────────────
    _check_vix(warnings, vix, ts)
    _check_iv_expansion(warnings, symbol_snapshots, ts)

    # ── 2. VWAP / Trend guards ───────────────────────────────────────────────
    _check_vwap_breach(warnings, symbol_snapshots, ts)
    _check_trend_guards(warnings, symbol_snapshots, ts)

    # ── 3. Time guards ───────────────────────────────────────────────────────
    _check_time_guards(warnings, now, ts, context)

    # ── 4. Risk guards ───────────────────────────────────────────────────────
    _check_risk_guards(warnings, context, now, ts)

    # ── 5. Signal quality guard ──────────────────────────────────────────────
    _check_signal_quality(warnings, symbol_snapshots, ts)

    # ── 6. Options structure guards ──────────────────────────────────────────
    _check_options_guards(warnings, symbol_snapshots, ts)

    # ── 6b. Blocked signal filter warnings ───────────────────────────────────
    _check_blocked_signals(warnings, symbol_snapshots, ts)

    # ── 7. Infra guard (stale data) ──────────────────────────────────────────
    _check_stale_data(warnings, symbol_snapshots, now, ts)

    # ── update ring buffer + active set ──────────────────────────────────────
    global _last_cycle_ids, _active_ids
    new_ids: set[str] = set()
    for w in warnings:
        _warning_history.append(w)
        new_ids.add(w["id"])
    _active_ids = new_ids | _last_cycle_ids
    _last_cycle_ids = new_ids

    # ── build market intel summary ────────────────────────────────────────────
    market_intel = _build_market_intel(symbol_snapshots, vix, context, now)

    return warnings, market_intel


def get_warning_history() -> list[dict[str, Any]]:
    return list(_warning_history)


def get_active_ids() -> set[str]:
    return set(_active_ids)


def clear_warnings() -> None:
    _warning_history.clear()
    _active_ids.clear()
    _last_cycle_ids.clear()


# ── guard implementations ─────────────────────────────────────────────────────

def _check_vix(warnings, vix: float | None, ts: str):
    if vix is None:
        return
    if vix > 22:
        warnings.append(_w(
            "vix_critical", "critical", "VOLATILITY",
            f"India VIX extremely elevated: {vix:.1f}",
            f"VIX above 22 signals severe market stress. Prefer defined-loss instruments (options). "
            f"Widen stops significantly or stay out.",
            ts=ts,
        ))
    elif vix > 18:
        warnings.append(_w(
            "vix_elevated", "warning", "VOLATILITY",
            f"India VIX elevated: {vix:.1f}",
            f"VIX between 18–22. Intraday ranges are wider than normal. "
            f"Prefer intraday_options or intraday_futures with conservative sizing.",
            ts=ts,
        ))
    elif vix < 12:
        warnings.append(_w(
            "vix_low", "info", "VOLATILITY",
            f"India VIX very low: {vix:.1f}",
            f"VIX below 12 — compressed market, low premium, low directional impulse. "
            f"RSI/range strategies preferred over momentum.",
            ts=ts,
        ))


def _check_iv_expansion(warnings, snapshots: dict, ts: str):
    for symbol, snap in snapshots.items():
        analytics = snap.get("analytics") or {}
        if not analytics:
            continue
        iv_change = analytics.get("iv_change_15m_pct")
        iv_pct = analytics.get("iv_percentile")
        if iv_change is not None and isinstance(iv_change, (int, float)) and iv_change > 25:
            warnings.append(_w(
                f"iv_expansion:{symbol}", "warning", "VOLATILITY",
                f"IV expanding rapidly: {symbol} +{iv_change:.0f}% / 15m",
                f"Implied volatility surged {iv_change:.0f}% in the last 15 minutes. "
                f"Options premiums are being repriced — wait for IV to stabilise before buying.",
                symbol=symbol, ts=ts,
            ))
        elif iv_change is not None and isinstance(iv_change, (int, float)) and iv_change < -20:
            warnings.append(_w(
                f"iv_crush:{symbol}", "warning", "VOLATILITY",
                f"IV crush detected: {symbol} {iv_change:.0f}% / 15m",
                f"IV dropped sharply — options buyers facing rapid premium erosion. "
                f"Buying strategies have significant headwind.",
                symbol=symbol, ts=ts,
            ))
        if iv_pct is not None and isinstance(iv_pct, (int, float)) and iv_pct > 85:
            warnings.append(_w(
                f"iv_high_pct:{symbol}", "info", "VOLATILITY",
                f"IV at high percentile: {symbol} ({iv_pct:.0f}th pct)",
                f"IV percentile {iv_pct:.0f} — options are expensive historically. "
                f"Selling strategies have edge; buying strategies face headwind.",
                symbol=symbol, ts=ts,
            ))


def _check_vwap_breach(warnings, snapshots: dict, ts: str):
    below_vwap_count = 0
    above_vwap_count = 0
    total = 0
    for snap in snapshots.values():
        vwap_bias = snap.get("vwap_bias")
        if vwap_bias is None:
            continue
        total += 1
        if vwap_bias == "below":
            below_vwap_count += 1
        elif vwap_bias == "above":
            above_vwap_count += 1

    if total >= 5:
        below_pct = below_vwap_count / total * 100
        above_pct = above_vwap_count / total * 100
        if below_pct >= 70:
            warnings.append(_w(
                "vwap_broad_weakness", "warning", "VWAP",
                f"Broad market weakness: {below_pct:.0f}% of symbols below VWAP",
                f"{below_vwap_count}/{total} scanned symbols are trading below VWAP. "
                f"Broad intraday selling pressure — prefer SELL signals; avoid new BUY entries.",
                ts=ts,
            ))
        elif above_pct >= 70:
            warnings.append(_w(
                "vwap_broad_strength", "info", "VWAP",
                f"Broad market strength: {above_pct:.0f}% of symbols above VWAP",
                f"{above_vwap_count}/{total} scanned symbols trading above VWAP — bullish breadth.",
                ts=ts,
            ))


def _check_trend_guards(warnings, snapshots: dict, ts: str):
    """Check EMA alignment across the scanned universe."""
    ema_bearish_count = 0
    ema_bullish_count = 0
    total_with_ema = 0
    for snap in snapshots.values():
        data = snap.get("data")
        if data is None or (hasattr(data, "empty") and data.empty) or len(data) < 50:
            continue
        try:
            close = data["Close"]
            ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
            ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
            total_with_ema += 1
            if ema20 < ema50:
                ema_bearish_count += 1
            else:
                ema_bullish_count += 1
        except Exception:
            continue

    if total_with_ema >= 5:
        bearish_pct = ema_bearish_count / total_with_ema * 100
        if bearish_pct >= 65:
            warnings.append(_w(
                "ema_bearish_alignment", "warning", "TREND",
                f"Bearish EMA alignment: {bearish_pct:.0f}% of symbols (EMA20 < EMA50)",
                f"{ema_bearish_count}/{total_with_ema} symbols have EMA20 below EMA50 — "
                f"HTF trend is down across the board. BUY signals carry higher counter-trend risk.",
                ts=ts,
            ))


def _check_time_guards(warnings, now: datetime, ts: str, context: Any):
    hour = now.hour
    minute = now.minute
    total_minutes = hour * 60 + minute

    entry_cutoff_minutes = 14 * 60  # default
    try:
        cfg = context.runtime_config
        cutoff_str = getattr(getattr(cfg, "session_defaults", None), "entry_cutoff_time", None) or "14:00"
        h, m = [int(x) for x in str(cutoff_str).split(":")]
        entry_cutoff_minutes = h * 60 + m
    except Exception:
        pass

    if entry_cutoff_minutes and total_minutes >= entry_cutoff_minutes - 15 and total_minutes < entry_cutoff_minutes:
        warnings.append(_w(
            "entry_cutoff_near", "warning", "TIME",
            f"Entry cutoff in <15 min ({entry_cutoff_minutes // 60:02d}:{entry_cutoff_minutes % 60:02d})",
            "New entries will be blocked once the cutoff is reached. "
            "Avoid entering new positions near the cutoff — time to profit is compressed.",
            ts=ts,
        ))

    if 14 * 60 + 50 <= total_minutes <= 15 * 60 + 5:
        warnings.append(_w(
            "square_off_approaching", "critical", "TIME",
            "MIS square-off approaching (15:10–15:15)",
            "Intraday (MIS) positions will be force-squared off at 15:10–15:15 by the broker. "
            "Close or monitor open positions closely.",
            ts=ts,
        ))

    # Lunch chop zone (12:00–13:00)
    if 12 * 60 <= total_minutes <= 13 * 60:
        warnings.append(_w(
            "lunch_chop_zone", "info", "TIME",
            "Lunch chop zone active (12:00–13:00)",
            "Market tends to be low-volume and choppy during the 12–1 PM window. "
            "False breakouts and whipsaws are more common. Prefer existing position management.",
            ts=ts,
        ))

    # Opening 15 minutes
    if 9 * 60 + 15 <= total_minutes <= 9 * 60 + 29:
        warnings.append(_w(
            "opening_volatility", "info", "TIME",
            "Opening 15 min — elevated noise, wide spreads",
            "First 15 minutes after open are noisy with thin liquidity. "
            "ATM options can have very wide spreads — check before entry.",
            ts=ts,
        ))

    # Late afternoon OTM risk (after 13:30)
    if total_minutes >= 13 * 60 + 30:
        # Check if any options position exists with OTM-like analytics
        try:
            for sym, snap in (symbol_snapshots := {}).items():
                pass  # checked below in options guards per-symbol
        except Exception:
            pass
        warnings.append(_w(
            "late_otm_risk", "info", "TIME",
            "Afternoon session: avoid fresh OTM buys after 13:30",
            "Post 1:30 PM, theta decay accelerates significantly for OTM options. "
            "Premium buyers face compounding time erosion — only trade with strong momentum.",
            ts=ts,
        ))


def _check_risk_guards(warnings, context: Any, now: datetime, ts: str):
    try:
        runtime_state = context.session_runtime_state
        risk_pause_until = runtime_state.get("risk_pause_until")
        risk_pause_reason = runtime_state.get("risk_pause_reason", "")
        if risk_pause_until:
            warnings.append(_w(
                "risk_pause_active", "critical", "RISK",
                f"Risk pause active: {risk_pause_reason or 'unknown reason'}",
                f"Trading is paused until {str(risk_pause_until)[:19]}. "
                f"No new entries will be placed. Reason: {risk_pause_reason}.",
                ts=ts,
            ))

        capital = float(getattr(context.config, "capital", 0) or 0)
        if capital > 0:
            trade_day = now.date()
            session_pnl = _calc_session_pnl(context.trade_book, trade_day)
            daily_loss_pct = (-session_pnl / capital * 100) if session_pnl < 0 else 0.0
            max_loss_pct = float(
                getattr(getattr(context.runtime_config, "risk_controls", None), "daily_max_loss_pct", 0.0) or 0.0
            ) * 100
            if max_loss_pct > 0 and daily_loss_pct >= max_loss_pct * 0.5:
                severity = "critical" if daily_loss_pct >= max_loss_pct * 0.8 else "warning"
                warnings.append(_w(
                    "daily_loss_approaching", severity, "RISK",
                    f"Daily loss {daily_loss_pct:.1f}% of {max_loss_pct:.1f}% limit",
                    f"Session P&L: ₹{session_pnl:,.0f}. You are at {daily_loss_pct:.1f}% of your "
                    f"{max_loss_pct:.1f}% daily loss limit. Tighten stops or avoid new entries.",
                    ts=ts,
                ))

        consecutive_losses = _calc_consecutive_losses(context.trade_book, now.date())
        consec_limit = int(
            getattr(getattr(context.runtime_config, "risk_controls", None), "consecutive_loss_limit", 0) or 0
        )
        if consecutive_losses >= 2:
            severity = "critical" if consec_limit > 0 and consecutive_losses >= consec_limit else "warning"
            warnings.append(_w(
                "consecutive_losses", severity, "RISK",
                f"{consecutive_losses} consecutive losing trades",
                f"Last {consecutive_losses} closed trades were losses. "
                f"{'Limit reached — new entries blocked. ' if severity == 'critical' else ''}"
                f"Consider reducing size or waiting for a cleaner setup.",
                ts=ts,
            ))
    except Exception:
        pass


def _check_signal_quality(warnings, snapshots: dict, ts: str):
    if not snapshots:
        return
    low_score_count = 0
    total = 0
    for snap in snapshots.values():
        score = snap.get("score")
        if score is None:
            continue
        total += 1
        if score < 0.3:
            low_score_count += 1

    if total >= 3 and low_score_count / total >= 0.7:
        warnings.append(_w(
            "low_signal_quality", "info", "SIGNAL",
            f"Weak signal quality: {low_score_count}/{total} symbols low score",
            f"{low_score_count} of {total} symbols have low signal scores (<0.3). "
            f"Market may be choppy or transitioning — high false-signal risk. "
            f"Consider extra confirmations or pausing new entries.",
            ts=ts,
        ))


def _check_options_guards(warnings, snapshots: dict, ts: str):
    for symbol, snap in snapshots.items():
        analytics = snap.get("analytics") or {}
        if not analytics:
            continue
        dte = analytics.get("days_to_expiry")
        if dte is not None and isinstance(dte, (int, float)) and dte == 0:
            warnings.append(_w(
                f"expiry_day:{symbol}", "warning", "OPTIONS",
                f"Expiry day: {symbol}",
                f"{symbol} expires today. Theta decay accelerates sharply — options lose value quickly "
                f"after 2 PM. Be cautious about buying premium this late in the session.",
                symbol=symbol, ts=ts,
            ))
        elif dte is not None and isinstance(dte, (int, float)) and dte == 1:
            warnings.append(_w(
                f"near_expiry:{symbol}", "info", "OPTIONS",
                f"Near expiry (1 DTE): {symbol}",
                f"{symbol} expires tomorrow. Gamma risk increases — positions can move violently.",
                symbol=symbol, ts=ts,
            ))


def _check_blocked_signals(warnings, snapshots: dict, ts: str):
    for symbol, snap in snapshots.items():
        note = snap.get("options_filter_note")
        signal = snap.get("signal", "")
        if not note or signal not in ("HOLD", None, ""):
            continue
        analytics = snap.get("analytics") or {}
        detail_parts = [str(note)]
        underlying = analytics.get("underlying")
        if isinstance(underlying, str) and underlying:
            detail_parts.append(f"Underlying: {underlying}")
        try:
            up = analytics.get("underlying_price")
            if up is not None:
                detail_parts.append(f"Spot: ₹{float(up):.2f}")
        except (TypeError, ValueError):
            pass
        opt_type = analytics.get("option_type")
        if isinstance(opt_type, str) and opt_type:
            detail_parts.append(f"Option type: {opt_type}")
        bias = analytics.get("underlying_bias")
        if isinstance(bias, str) and bias:
            detail_parts.append(f"Underlying bias: {bias}")
        rejection_code = snap.get("rejection_code")
        if isinstance(rejection_code, str) and rejection_code:
            detail_parts.append(f"Reason code: {rejection_code}")
        entry_quality = snap.get("entry_quality") or {}
        delta = entry_quality.get("delta")
        if delta is not None:
            try:
                detail_parts.append(f"Delta: {float(delta):.3f}")
            except (TypeError, ValueError):
                pass
        regime_label = entry_quality.get("volatility_regime")
        if isinstance(regime_label, str) and regime_label and regime_label != "UNKNOWN":
            detail_parts.append(f"Regime: {regime_label}")
        detail = " | ".join(detail_parts)
        short_sym = symbol.split(":")[-1] if ":" in symbol else symbol
        warnings.append(_w(
            f"options_blocked:{symbol}", "warning", "OPTIONS",
            f"Entry blocked: {short_sym}",
            detail,
            symbol=symbol, ts=ts,
        ))


def _check_stale_data(warnings, snapshots: dict, now: datetime, ts: str):
    stale_symbols = []
    for symbol, snap in snapshots.items():
        data = snap.get("data")
        if data is None or (hasattr(data, "empty") and data.empty):
            continue
        try:
            latest_ts = data.index[-1].to_pydatetime()
            if latest_ts.tzinfo is not None:
                latest_ts = latest_ts.astimezone().replace(tzinfo=None)
            age_minutes = (now - latest_ts).total_seconds() / 60.0
            if age_minutes > 15:
                stale_symbols.append((symbol, age_minutes))
        except Exception:
            pass

    if stale_symbols:
        worst = max(stale_symbols, key=lambda x: x[1])
        warnings.append(_w(
            "stale_data", "warning", "INFRA",
            f"Stale candle data: {len(stale_symbols)} symbol(s) >15 min old",
            f"Data for {', '.join(s for s, _ in stale_symbols[:3])}{'...' if len(stale_symbols) > 3 else ''} "
            f"is stale (worst: {worst[0]} at {worst[1]:.0f} min old). "
            f"Possible causes: provider lag, internet issue, rate limiting.",
            ts=ts,
        ))


# ── signal quality score (0–100) ──────────────────────────────────────────────

def compute_signal_quality_score(
    snapshots: dict[str, Any],
    vix: float | None,
    now: datetime,
) -> dict[str, Any]:
    """
    Compute a 0–100 signal quality score from available scan data.

    Factors (weights):
    - VWAP alignment (20 pts)
    - EMA alignment (15 pts)
    - ADX strength (15 pts)
    - Signal score distribution (20 pts)
    - IV stability (15 pts)
    - VIX environment (15 pts)

    Returns dict with score, label, breakdown.
    """
    if not snapshots:
        return {"score": 0, "label": "NO DATA", "breakdown": {}}

    pts = {}

    # 1. VWAP alignment (20 pts)
    above = sum(1 for s in snapshots.values() if s.get("vwap_bias") == "above")
    below = sum(1 for s in snapshots.values() if s.get("vwap_bias") == "below")
    total_vwap = above + below
    if total_vwap > 0:
        dominant_pct = max(above, below) / total_vwap
        pts["vwap"] = round(dominant_pct * 20)
    else:
        pts["vwap"] = 10

    # 2. EMA alignment (15 pts)
    ema_aligned = 0
    ema_total = 0
    for snap in snapshots.values():
        data = snap.get("data")
        if data is None or (hasattr(data, "empty") and data.empty) or len(data) < 50:
            continue
        try:
            close = data["Close"]
            ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
            ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
            ema_total += 1
            if ema20 > ema50:
                ema_aligned += 1
        except Exception:
            pass
    if ema_total > 0:
        pts["ema"] = round((max(ema_aligned, ema_total - ema_aligned) / ema_total) * 15)
    else:
        pts["ema"] = 7

    # 3. ADX strength (15 pts)
    adx_values = []
    for snap in snapshots.values():
        data = snap.get("data")
        adx = _compute_adx_latest(data)
        if adx is not None:
            adx_values.append(adx)
    if adx_values:
        avg_adx = sum(adx_values) / len(adx_values)
        # ADX > 25 = trending, > 40 = strong trend
        if avg_adx >= 40:
            pts["adx"] = 15
        elif avg_adx >= 25:
            pts["adx"] = round(10 + (avg_adx - 25) / 15 * 5)
        elif avg_adx >= 15:
            pts["adx"] = round((avg_adx - 15) / 10 * 10)
        else:
            pts["adx"] = 0
    else:
        pts["adx"] = 7

    # 4. Signal score distribution (20 pts)
    scores = [s.get("score") for s in snapshots.values() if s.get("score") is not None]
    if scores:
        avg_score = sum(scores) / len(scores)
        high_count = sum(1 for s in scores if s >= 0.6)
        pts["signal"] = round(min(20, avg_score * 20 + high_count * 2))
    else:
        pts["signal"] = 5

    # 5. IV stability (15 pts) — penalise rapid IV changes
    iv_change_penalty = 0
    iv_checked = 0
    for snap in snapshots.values():
        analytics = snap.get("analytics") or {}
        iv_change = analytics.get("iv_change_15m_pct")
        if iv_change is not None and isinstance(iv_change, (int, float)):
            iv_checked += 1
            iv_change_penalty += min(1.0, abs(iv_change) / 50.0)
    if iv_checked > 0:
        avg_penalty = iv_change_penalty / iv_checked
        pts["iv_stability"] = round((1 - avg_penalty) * 15)
    else:
        pts["iv_stability"] = 10  # neutral when no options data

    # 6. VIX environment (15 pts)
    if vix is None:
        pts["vix_env"] = 8
    elif vix < 14:
        pts["vix_env"] = 15
    elif vix < 18:
        pts["vix_env"] = 10
    elif vix < 22:
        pts["vix_env"] = 5
    else:
        pts["vix_env"] = 0

    score = min(100, sum(pts.values()))

    if score >= 80:
        label = "AGGRESSIVE"
    elif score >= 60:
        label = "NORMAL"
    elif score >= 40:
        label = "CAUTION"
    else:
        label = "AVOID"

    return {"score": score, "label": label, "breakdown": pts}


# ── market intel builder ──────────────────────────────────────────────────────

def _build_market_intel(
    snapshots: dict,
    vix: float | None,
    context: Any,
    now: datetime,
) -> dict[str, Any]:
    """Aggregate market-level intelligence from scan data for the UI panel."""
    total = len(snapshots)
    above_vwap = sum(1 for s in snapshots.values() if s.get("vwap_bias") == "above")
    below_vwap = sum(1 for s in snapshots.values() if s.get("vwap_bias") == "below")

    buy_signals  = sum(1 for s in snapshots.values() if s.get("signal") in {"BUY", "BUY_CE"})
    sell_signals = sum(1 for s in snapshots.values() if s.get("signal") in {"SELL", "BUY_PE"})
    hold_signals = total - buy_signals - sell_signals

    scores = [s.get("score") for s in snapshots.values() if s.get("score") is not None]
    avg_score = round(sum(scores) / len(scores), 3) if scores else None

    # EMA alignment breadth
    ema_above = 0
    ema_below = 0
    for snap in snapshots.values():
        data = snap.get("data")
        if data is None or (hasattr(data, "empty") and data.empty) or len(data) < 50:
            continue
        try:
            close = data["Close"]
            ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
            ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
            if ema20 > ema50:
                ema_above += 1
            else:
                ema_below += 1
        except Exception:
            pass

    # ADX average
    adx_values = []
    for snap in snapshots.values():
        adx = _compute_adx_latest(snap.get("data"))
        if adx is not None:
            adx_values.append(adx)
    avg_adx = round(sum(adx_values) / len(adx_values), 1) if adx_values else None

    # Regime detection (enhanced)
    regime = _detect_regime_enhanced(snapshots, vix, avg_adx)

    # Signal quality score
    sq = compute_signal_quality_score(snapshots, vix, now)

    # Best IV snapshot
    atm_iv = None
    atm_iv_pct = None
    atm_symbol = None
    atm_iv_change = None
    for sym, snap in snapshots.items():
        analytics = snap.get("analytics") or {}
        try:
            iv_raw = analytics.get("iv")
            if iv_raw is None:
                continue
            atm_iv = round(float(iv_raw) * 100, 1)
            iv_pct_raw = analytics.get("iv_percentile")
            atm_iv_pct = round(float(iv_pct_raw), 0) if iv_pct_raw is not None else None
            iv_chg_raw = analytics.get("iv_change_15m_pct")
            atm_iv_change = round(float(iv_chg_raw), 1) if iv_chg_raw is not None else None
            atm_symbol = sym
            break
        except (TypeError, ValueError):
            continue

    # Gap % from first symbol that has enough daily history
    gap_pct = None
    for snap in snapshots.values():
        data = snap.get("data")
        if data is not None and not (hasattr(data, "empty") and data.empty) and len(data) >= 2:
            try:
                today_open  = float(data["Open"].iloc[0])
                prev_close  = float(data["Close"].iloc[-2])
                if prev_close > 0:
                    gap_pct = round((today_open - prev_close) / prev_close * 100, 2)
                break
            except Exception:
                pass

    # Intraday trend label from VWAP + EMA consensus
    intraday_trend = "NEUTRAL"
    ema_total = ema_above + ema_below
    if ema_total > 0:
        if ema_above / ema_total >= 0.65 and above_vwap > below_vwap:
            intraday_trend = "BULLISH"
        elif ema_below / ema_total >= 0.65 and below_vwap > above_vwap:
            intraday_trend = "BEARISH"
        elif ema_above / ema_total >= 0.55:
            intraday_trend = "MILD BULLISH"
        elif ema_below / ema_total >= 0.55:
            intraday_trend = "MILD BEARISH"

    return {
        "total_symbols": total,
        "above_vwap": above_vwap,
        "below_vwap": below_vwap,
        "ema_above": ema_above,
        "ema_below": ema_below,
        "buy_signals": buy_signals,
        "sell_signals": sell_signals,
        "hold_signals": hold_signals,
        "avg_score": avg_score,
        "avg_adx": avg_adx,
        "regime": regime,
        "intraday_trend": intraday_trend,
        "vix": round(float(vix), 1) if vix is not None else None,
        "gap_pct": gap_pct,
        "atm_iv": atm_iv,
        "atm_iv_pct": atm_iv_pct,
        "atm_iv_change": atm_iv_change,
        "atm_symbol": atm_symbol,
        "signal_quality": sq,
    }


def _detect_regime_enhanced(snapshots: dict, vix: float | None, avg_adx: float | None) -> str:
    total = len(snapshots)
    if total == 0:
        return "UNKNOWN"
    above_vwap = sum(1 for s in snapshots.values() if s.get("vwap_bias") == "above")
    below_vwap = sum(1 for s in snapshots.values() if s.get("vwap_bias") == "below")
    scores = [s.get("score") for s in snapshots.values() if s.get("score") is not None]
    avg_score = sum(scores) / len(scores) if scores else 0.5
    adx = avg_adx or 0

    if vix is not None and vix > 22:
        return "HIGH_VOLATILITY"
    if adx >= 30 and above_vwap / total >= 0.6 and avg_score >= 0.5:
        return "TREND_DAY"
    if adx >= 30 and below_vwap / total >= 0.6 and avg_score >= 0.5:
        return "TREND_DAY"
    if adx >= 25 and above_vwap / total >= 0.65:
        return "TREND_UP"
    if adx >= 25 and below_vwap / total >= 0.65:
        return "TREND_DOWN"
    if vix is not None and vix > 18:
        return "HIGH_VOL"
    if adx < 15 and avg_score < 0.35:
        return "CHOPPY"
    if adx < 20:
        return "RANGE_BOUND"
    return "RANGE_BOUND"


# ── ADX computation ───────────────────────────────────────────────────────────

def _compute_adx_latest(data, period: int = 14) -> float | None:
    """Compute the latest ADX value from a candle DataFrame."""
    if data is None or (hasattr(data, "empty") and data.empty) or len(data) < period * 2:
        return None
    try:
        high  = data["High"]
        low   = data["Low"]
        close = data["Close"]

        up_move   = high.diff()
        down_move = -low.diff()

        plus_dm  = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low  - close.shift(1)).abs(),
        ], axis=1).max(axis=1)

        atr     = tr.ewm(span=period, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, float("nan"))
        minus_di= 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, float("nan"))
        dx      = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan")))
        adx     = dx.ewm(span=period, adjust=False).mean()

        val = float(adx.iloc[-1])
        return None if val != val else round(val, 1)
    except Exception:
        return None


# ── helpers ───────────────────────────────────────────────────────────────────

def _w(
    wid: str,
    severity: str,
    category: str,
    title: str,
    detail: str,
    symbol: str | None = None,
    ts: str = "",
) -> dict[str, Any]:
    return {
        "id": wid,
        "severity": severity,
        "category": category,
        "title": title,
        "detail": detail,
        "symbol": symbol,
        "ts": ts,
    }


def _calc_session_pnl(trade_book, trade_day) -> float:
    day_str = trade_day.isoformat()
    total = 0.0
    for t in trade_book or []:
        exit_time = t.get("exit_time")
        if not exit_time:
            continue
        if str(exit_time)[:10] != day_str:
            continue
        total += float(t.get("net_pnl", t.get("pnl", 0)) or 0)
    return total


def _calc_consecutive_losses(trade_book, trade_day) -> int:
    day_str = trade_day.isoformat()
    today_trades = [
        t for t in (trade_book or [])
        if str(t.get("exit_time", ""))[:10] == day_str
    ]
    count = 0
    for t in reversed(today_trades):
        if float(t.get("net_pnl", t.get("pnl", 0)) or 0) < 0:
            count += 1
        else:
            break
    return count
