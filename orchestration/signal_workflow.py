from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from fno_data_fetcher import get_atm_option_strike, get_option_greeks_snapshot, resolve_option_contract
from signal_scoring import evaluate_symbol_signal, get_atr_value, rank_candidates

from . import positions as position_flow


@dataclass
class SignalScanResult:
    symbol_snapshots: dict[str, dict[str, Any]]
    ranked_candidates: list[dict[str, Any]]


def log_market_context(
    log_event: Any,
    symbol: str,
    context: dict[str, Any],
) -> None:
    log_event(
        f"[MARKET] {symbol} | Gap={context['gap_percent']:.2f}% | GapType={context['gap_type']} | Behavior={context['behavior']} | Strategies={context['strategies']} | MinConf={context['min_confirmations']} | AllowEntries={context['allow_entries']}"
    )
    if context.get("reason"):
        log_event(f"[MARKET] {symbol} | {context['reason']}")


def get_stable_signal_data(engine: Any, data: Any, now: datetime) -> Any:
    if data.empty or len(data) < 2:
        return data

    latest_ts = data.index[-1]
    latest_naive = latest_ts.to_pydatetime().replace(tzinfo=None)
    current_minute = now.replace(second=0, microsecond=0)
    if latest_naive >= current_minute and getattr(engine, "require_closed_signal_candle", False):
        return data.iloc[:-1]
    return data


def resolve_atm_option_contract_snapshot(
    engine: Any,
    atm_option_config: dict[str, Any],
    evaluation: dict[str, Any],
    now: datetime,
    fetch_data: Any,
) -> dict[str, Any] | None:
    option_type = evaluation.get("option_type")
    if option_type not in {"CE", "PE"}:
        return None

    underlying = atm_option_config["underlying"]
    expiry = atm_option_config["expiry"]
    strike_offset = int(atm_option_config.get("strike_offset", 0))
    strike = get_atm_option_strike(
        underlying,
        expiry,
        option_type,
        strike_offset=strike_offset,
    )
    contract_symbol = resolve_option_contract(underlying, expiry, strike, option_type)
    option_data = fetch_data(
        contract_symbol,
        period=engine.data_period,
        interval=engine.data_interval,
    )
    stable_option_data = get_stable_signal_data(engine, option_data, now)
    if stable_option_data.empty:
        raise RuntimeError(f"No option candles returned for {contract_symbol}")

    latest_candle = stable_option_data.iloc[-1]
    latest_close = float(latest_candle["Close"])
    return {
        "symbol": contract_symbol,
        "strike": strike,
        "option_type": option_type,
        "data": stable_option_data,
        "latest_candle": latest_candle,
        "latest_close": latest_close,
        "atr": get_atr_value(stable_option_data),
        "analytics": get_option_greeks_snapshot(contract_symbol),
        "trade_identity": underlying,
        "strike_offset": strike_offset,
        "strike_offset_mode": atm_option_config.get("strike_offset_mode", "ATM"),
    }


def get_cached_regime_context(
    regime_cache: dict[str, Any],
    symbol: str,
    trade_day: date,
) -> dict[str, Any] | None:
    cached = regime_cache.get(symbol)
    if not cached:
        return None
    if cached.get("trade_day") != trade_day.isoformat():
        return None
    return cached.get("context")


def scan_symbols(context: Any, now: datetime) -> SignalScanResult:
    cfg = context.config
    engine = context.engine
    current_trade_day = now.date()
    symbol_snapshots = {}
    candidates = []
    symbols_to_refresh = list(dict.fromkeys(cfg.selected_symbols + list(context.positions.keys())))

    for symbol in symbols_to_refresh:
        fetch_started_at = time.time()
        try:
            data = context.fetch_data(
                symbol,
                period=engine.data_period,
                interval=engine.data_interval,
            )
        except Exception as exc:
            context.log_event(
                f"[ERROR] Data fetch failed for {symbol} | Provider={cfg.data_provider} | Engine={engine.name} | {type(exc).__name__}: {exc}",
                "error",
            )
            context.logger.exception(
                "[ERROR] Exception during get_data for %s | Provider=%s | Engine=%s",
                symbol,
                cfg.data_provider,
                engine.name,
            )
            continue

        fetch_elapsed = time.time() - fetch_started_at
        if fetch_elapsed > 15:
            context.log_event(
                f"[HEALTH] Slow data fetch for {symbol}: {fetch_elapsed:.2f}s (provider={cfg.data_provider}, period={engine.data_period}, interval={engine.data_interval})",
                "warning",
            )

        if data.empty:
            context.log_event(
                f"[ERROR] No data for {symbol} | Provider={cfg.data_provider} | Possible causes: provider returned empty candles, market-data outage, internet issue, symbol issue, or request throttling",
                "error",
            )
            continue

        latest_candle = data.iloc[-1]
        latest_close = float(latest_candle["Close"])
        latest_timestamp = data.index[-1]
        candle_age_minutes = None
        try:
            latest_ts = latest_timestamp.to_pydatetime()
            if latest_ts.tzinfo is not None:
                latest_ts = latest_ts.astimezone().replace(tzinfo=None)
            candle_age_minutes = (now - latest_ts).total_seconds() / 60.0
        except Exception:
            candle_age_minutes = None
        if candle_age_minutes is not None and candle_age_minutes > 10:
            context.log_event(
                f"[DATA WARNING] Stale candle for {symbol}: last candle at {latest_timestamp} ({candle_age_minutes:.1f} minutes old). Possible causes: provider lag, stalled feed, or delayed internet.",
                "warning",
            )

        signal_data = get_stable_signal_data(engine, data, now)
        if signal_data.empty:
            context.log_event(
                f"[SCAN] {symbol} has no fully closed candle available yet, skipping signal evaluation",
                "warning",
            )
            continue

        active_mode = cfg.mode
        active_strategy_name = cfg.strategy_name
        active_strategies = cfg.strategies
        active_min_confirmations = cfg.min_confirmations
        market_context = None
        intraday_history = None
        option_analytics = None
        candidate_symbol = symbol
        candidate_latest_close = latest_close
        candidate_latest_candle = latest_candle
        candidate_atr = get_atr_value(signal_data)
        trade_identity = symbol
        dynamic_atm_scan = (
            engine.name == "intraday_options"
            and cfg.atm_option_config is not None
            and symbol == cfg.atm_option_config["scan_symbol"]
        )
        contract_data = signal_data

        if (
            engine.name == "intraday_equity"
            and engine.requires_extended_intraday_history(
                cfg.mode,
                strategy_name=cfg.strategy_name,
                strategies=cfg.strategies,
            )
        ):
            intraday_history = context.fetch_data(symbol, period="5d", interval="1m")

        if cfg.mode == "3" and engine.name == "intraday_equity":
            market_context = get_cached_regime_context(
                context.regime_cache,
                symbol,
                current_trade_day,
            )
            if market_context is None:
                daily_data = context.fetch_data(symbol, period="5d", interval="1d")
                market_context = engine.build_market_context(symbol, data, daily_data)
                if market_context.get("cacheable"):
                    context.regime_cache[symbol] = {
                        "trade_day": current_trade_day.isoformat(),
                        "context": market_context,
                    }
                    from .context import persist_runtime_state

                    persist_runtime_state(context)
            log_market_context(context.log_event, symbol, market_context)
            active_mode = "2"
            active_strategy_name = None
            active_strategies = market_context["strategies"]
            active_min_confirmations = market_context["min_confirmations"]

        if engine.name == "intraday_options" and cfg.atm_option_config and not dynamic_atm_scan:
            evaluation = {
                "signal": "HOLD",
                "agreement_count": 0,
                "score": 0.0,
                "details": {},
                "reason": "ATM option positions are managed by exits; new signals come from the underlying",
                "option_signal": None,
                "option_type": None,
                "strength": 0.0,
            }
        else:
            evaluation = evaluate_symbol_signal(
                signal_data,
                active_mode,
                strategy_name=active_strategy_name,
                strategies=active_strategies,
                min_confirmations=active_min_confirmations,
            )

        if dynamic_atm_scan and evaluation.get("option_signal") in {"BUY_CE", "BUY_PE"}:
            try:
                contract_snapshot = resolve_atm_option_contract_snapshot(
                    engine,
                    cfg.atm_option_config,
                    evaluation,
                    now,
                    context.fetch_data,
                )
                candidate_symbol = contract_snapshot["symbol"]
                candidate_latest_close = contract_snapshot["latest_close"]
                candidate_latest_candle = contract_snapshot["latest_candle"]
                candidate_atr = contract_snapshot["atr"]
                trade_identity = contract_snapshot["trade_identity"]
                option_analytics = contract_snapshot["analytics"]
                contract_data = contract_snapshot["data"]
                context.log_event(
                    f"[ATM] {symbol} -> {evaluation['option_signal']} -> {candidate_symbol} | Premium={candidate_latest_close:.2f}"
                )
            except Exception as exc:
                context.log_event(f"[ATM] Could not resolve ATM contract for {symbol}: {exc}", "warning")
                evaluation["signal"] = "HOLD"
                evaluation["agreement_count"] = 0
                evaluation["score"] = 0.0
                evaluation["reason"] = str(exc)
        elif "options" in engine.name:
            try:
                option_analytics = get_option_greeks_snapshot(symbol)
                if (
                    engine.name == "intraday_options"
                    and cfg.option_pair_config
                    and symbol in cfg.option_pair_config.get("symbols", [])
                ):
                    option_analytics["skip_underlying_bias"] = True
            except Exception as exc:
                context.log_event(f"[GREEKS] Could not build options analytics for {symbol}: {exc}", "warning")

        if hasattr(engine, "apply_signal_filters"):
            evaluation = engine.apply_signal_filters(
                evaluation,
                contract_data,
                intraday_history_df=intraday_history,
                min_confirmations=active_min_confirmations or 1,
                analytics=option_analytics,
            )
            if getattr(engine, "runtime_state_dirty", False):
                from .context import persist_runtime_state

                persist_runtime_state(context)
                engine.runtime_state_dirty = False

        symbol_snapshots[symbol] = {
            "data": data,
            "latest_candle": candidate_latest_candle,
            "latest_close": latest_close,
            "signal": evaluation["signal"],
            "agreement_count": evaluation["agreement_count"],
            "score": evaluation["score"],
            "details": evaluation["details"],
            "market_context": market_context,
            "vwap_bias": evaluation.get("vwap_bias"),
            "breakout_volume_note": evaluation.get("breakout_volume_note"),
            "options_filter_note": evaluation.get("options_filter_note"),
            "analytics": option_analytics,
            "atr": candidate_atr,
            "reason": evaluation.get("reason"),
        }

        context.log_event(
            f"[SCAN] {symbol} | Signal={evaluation['signal']} | Agree={evaluation['agreement_count']} | Score={evaluation['score']:.4f} | ATR={symbol_snapshots[symbol]['atr']:.2f} | Last close={candidate_latest_close:.2f} | VWAP bias={evaluation.get('vwap_bias', 'N/A')} | Range%={evaluation.get('range_pct', 0.0):.2f} | Underlying bias={evaluation.get('underlying_bias', 'N/A')}"
        )
        if evaluation.get("reason"):
            context.log_event(f"[SCAN] {symbol} | Reason: {evaluation['reason']}")
        if evaluation.get("breakout_volume_note"):
            context.log_event(f"[SCAN] {symbol} | Breakout volume filter: {evaluation['breakout_volume_note']}")
        if option_analytics:
            context.log_event(
                f"[GREEKS] {symbol} | Underlying={option_analytics['underlying_price']:.2f} | Premium={option_analytics['option_price']:.2f} | IV={option_analytics['iv']:.4f} | IV15m={option_analytics.get('iv_change_15m_pct', 'N/A')} | Delta={option_analytics['delta']:.4f} | Gamma={option_analytics['gamma']:.6f} | Theta={option_analytics['theta']:.4f} | Vega={option_analytics['vega']:.4f} | DTE={option_analytics.get('days_to_expiry', 'N/A')} | IVPct={option_analytics['iv_percentile'] if option_analytics['iv_percentile'] is not None else 'N/A'}"
            )
        if evaluation.get("options_filter_note"):
            context.log_event(f"[SCAN] {symbol} | Options filter: {evaluation['options_filter_note']}")

        allow_symbol_entries = True
        if market_context is not None:
            allow_symbol_entries = market_context["allow_entries"]

        normalized_signal = engine.normalize_entry_signal(evaluation["signal"])
        if normalized_signal and not allow_symbol_entries:
            context.log_event(f"[LIMIT] {symbol} adaptive mode not ready for entries yet")
            normalized_signal = None
        if (
            normalized_signal
            and engine.name == "intraday_options"
            and cfg.option_pair_config
            and symbol in cfg.option_pair_config.get("symbols", [])
        ):
            normalized_signal = None
        if normalized_signal:
                candidates.append(
                {
                    "symbol": candidate_symbol,
                    "signal": normalized_signal,
                    "agreement_count": evaluation["agreement_count"],
                    "score": evaluation["score"],
                    "latest_close": candidate_latest_close,
                    "atr": symbol_snapshots[symbol]["atr"],
                    "analytics": option_analytics,
                    "trade_identity": trade_identity,
                    "underlying_signal": evaluation.get("option_signal"),
                    "strike_offset": (
                        contract_snapshot["strike_offset"] if dynamic_atm_scan else None
                    ),
                    "strike_offset_mode": (
                        contract_snapshot["strike_offset_mode"] if dynamic_atm_scan else None
                    ),
                }
            )

    if engine.name == "intraday_options" and cfg.option_pair_config:
        pair_candidate = position_flow.build_option_pair_candidate(
            engine,
            cfg.option_pair_config,
            symbol_snapshots,
            context.positions,
            context.log_event,
        )
        if pair_candidate:
            candidates.append(pair_candidate)

    return SignalScanResult(
        symbol_snapshots=symbol_snapshots,
        ranked_candidates=rank_candidates(candidates),
    )
