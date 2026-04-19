import sys
import time
from datetime import date, datetime

from config import (
    MANUAL_SYMBOL_TABLE,
    NIFTY50_SYMBOLS,
    SINGLE_SYMBOL_TABLE,
)
from data_fetcher import get_data, set_data_provider
from engines import (
    DeliveryEquityEngine,
    FuturesEquityEngine,
    IntradayEquityEngine,
    OptionsEquityEngine,
)
from fno_data_fetcher import (
    FNO_INDEX_SYMBOLS,
    get_atm_option_strike,
    get_available_expiries,
    get_available_option_strikes,
    get_fno_display_name,
    resolve_futures_contract,
    resolve_option_contract,
)
from engines.common import (
    apply_capital_limits_to_quantity,
    build_position,
    get_deployed_capital,
    log_positions,
    update_trailing_stop,
)
from executor import place_order, set_execution_mode, set_execution_provider
from logger import finalize_session_logger, log_event, setup_session_logger
from risk_manager import (
    atr_position_size,
    atr_stop_from_value,
    calculate_target_price,
)
from signal_scoring import (
    evaluate_symbol_signal,
    get_atr_value,
    rank_candidates,
)
from state_store import load_engine_state, save_engine_state

sys.stdout.reconfigure(encoding="utf-8")

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

DEFAULT_CONFIRMATIONS = {
    2: 2,
    3: 2,
    4: 3,
    5: 3,
}

ENGINE_OPTIONS = {
    "1": IntradayEquityEngine,
    "2": DeliveryEquityEngine,
    "3": FuturesEquityEngine,
    "4": OptionsEquityEngine,
}


def prompt_float(message, default=None, minimum=None, maximum=None):
    while True:
        raw = input(message).strip()

        if not raw and default is not None:
            value = float(default)
        else:
            try:
                value = float(raw)
            except ValueError:
                log_event("[INPUT] Enter a valid number.", "warning")
                continue

        if minimum is not None and value < minimum:
            log_event(f"[INPUT] Value must be at least {minimum}.", "warning")
            continue

        if maximum is not None and value > maximum:
            log_event(f"[INPUT] Value must be at most {maximum}.", "warning")
            continue

        return value


def prompt_int(message, default=None, minimum=None, maximum=None):
    while True:
        raw = input(message).strip()

        if not raw and default is not None:
            value = int(default)
        else:
            try:
                value = int(raw)
            except ValueError:
                log_event("[INPUT] Enter a valid whole number.", "warning")
                continue

        if minimum is not None and value < minimum:
            log_event(f"[INPUT] Value must be at least {minimum}.", "warning")
            continue

        if maximum is not None and value > maximum:
            log_event(f"[INPUT] Value must be at most {maximum}.", "warning")
            continue

        return value


def prompt_choice(message, valid_choices, default=None):
    normalized = {
        str(choice["key"]): choice["value"].upper()
        for choice in valid_choices
    }
    display = ", ".join(
        f"{choice['label']}:{choice['key']}" for choice in valid_choices
    )

    while True:
        raw = input(message).strip()

        if not raw and default is not None:
            raw = str(default)

        if raw in normalized:
            return normalized[raw]

        log_event(f"[INPUT] Choose one of: {display}.", "warning")


def normalize_symbol(raw_symbol):
    symbol = raw_symbol.strip().upper()
    if not symbol:
        return ""
    if not symbol.endswith(".NS"):
        symbol = f"{symbol}.NS"
    return symbol


def prompt_symbol_selection():
    log_event("[SETUP] Symbol selection - which stocks to scan for signals")
    log_event("[SETUP]   SINGLE: Scan only 1 stock (good for testing specific stocks)")
    log_event("[SETUP]   MANUAL MULTI: Choose multiple stocks from a table")
    log_event("[SETUP]   NIFTY50 UNIVERSE: Scan all 50 NIFTY stocks (comprehensive)")

    symbol_mode = prompt_choice(
        "Symbol mode: SINGLE(1), MANUAL MULTI(2), NIFTY50 UNIVERSE(3)? [default 3]: ",
        [
            {"label": "SINGLE", "key": 1, "value": "SINGLE"},
            {"label": "MANUAL MULTI", "key": 2, "value": "MANUAL_MULTI"},
            {"label": "NIFTY50 UNIVERSE", "key": 3, "value": "NIFTY50"},
        ],
        default=3,
    )

    if symbol_mode == "SINGLE":
        selection_mode = prompt_choice(
            "Single symbol selection: USE TABLE(1) or TYPE SYMBOL(2)? [default 1]: ",
            [
                {"label": "USE TABLE", "key": 1, "value": "TABLE"},
                {"label": "TYPE SYMBOL", "key": 2, "value": "TYPE"},
            ],
            default=1,
        )

        if selection_mode == "TABLE":
            log_event("Single symbol table:")
            for key, symbol in SINGLE_SYMBOL_TABLE.items():
                log_event(f"{key}. {symbol}")

            while True:
                raw = input("Enter single-symbol table number: ").strip()
                if raw in SINGLE_SYMBOL_TABLE:
                    symbol = SINGLE_SYMBOL_TABLE[raw]
                    log_event(f"[MAIN] Symbol selected: {symbol}")
                    return [symbol], symbol_mode

                log_event("[INPUT] Choose a valid single-symbol table number.", "warning")

        while True:
            raw = input("Enter symbol (example: RELIANCE): ").strip()
            symbol = normalize_symbol(raw)
            if symbol:
                log_event(f"[MAIN] Symbol selected: {symbol}")
                return [symbol], symbol_mode
            log_event("[INPUT] Enter a valid symbol.", "warning")

    if symbol_mode == "MANUAL_MULTI":
        selection_mode = prompt_choice(
            "Manual symbol selection: TYPE SYMBOLS(1) or USE TABLE(2)? [default 2]: ",
            [
                {"label": "TYPE SYMBOLS", "key": 1, "value": "TYPE"},
                {"label": "USE TABLE", "key": 2, "value": "TABLE"},
            ],
            default=2,
        )

        if selection_mode == "TABLE":
            log_event("Manual symbol table:")
            for key, symbol in MANUAL_SYMBOL_TABLE.items():
                log_event(f"{key}. {symbol}")

            while True:
                raw = input("Enter table numbers separated by commas: ").strip()
                selected_keys = [
                    item.strip()
                    for item in raw.split(",")
                    if item.strip()
                ]

                if not selected_keys:
                    log_event("[INPUT] Select at least one symbol.", "warning")
                    continue

                invalid = [
                    key for key in selected_keys if key not in MANUAL_SYMBOL_TABLE
                ]
                if invalid:
                    log_event(
                        f"[INPUT] Invalid table numbers: {', '.join(invalid)}",
                        "warning",
                    )
                    continue

                symbols = []
                seen = set()
                for key in selected_keys:
                    symbol = MANUAL_SYMBOL_TABLE[key]
                    if symbol not in seen:
                        symbols.append(symbol)
                        seen.add(symbol)

                log_event(f"[MAIN] Symbols selected: {symbols}")
                return symbols, symbol_mode

        while True:
            raw = input(
                "Enter symbols separated by commas "
                "(example: RELIANCE,INFY,TCS): "
            ).strip()
            selected = [
                normalize_symbol(item)
                for item in raw.split(",")
                if item.strip()
            ]
            selected = [symbol for symbol in selected if symbol]

            if not selected:
                log_event("[INPUT] Select at least one symbol.", "warning")
                continue

            symbols = []
            seen = set()
            for symbol in selected:
                if symbol not in seen:
                    symbols.append(symbol)
                    seen.add(symbol)

            log_event(f"[MAIN] Symbols selected: {symbols}")
            return symbols, symbol_mode

    log_event(
        f"[MAIN] Using NIFTY50 universe with {len(NIFTY50_SYMBOLS)} symbols"
    )
    return list(NIFTY50_SYMBOLS), symbol_mode


def prompt_fno_base_symbols(engine_name):
    log_event("[SETUP] F&O underlying selection - choose your derivatives universe")
    for index, symbol in enumerate(FNO_INDEX_SYMBOLS, start=1):
        log_event(f"[SETUP]   {get_fno_display_name(symbol)} ({index})")

    if engine_name == "futures_equity":
        return prompt_choice(
            "F&O futures universe: NIFTY 50(1), SENSEX(2), BOTH(3) [default 3]: ",
            [
                {"label": get_fno_display_name(FNO_INDEX_SYMBOLS[0]), "key": 1, "value": FNO_INDEX_SYMBOLS[0]},
                {"label": get_fno_display_name(FNO_INDEX_SYMBOLS[1]), "key": 2, "value": FNO_INDEX_SYMBOLS[1]},
                {"label": "BOTH", "key": 3, "value": "BOTH"},
            ],
            default=3,
        )

    return prompt_choice(
        "F&O options underlying: " + ", ".join(
            f"{get_fno_display_name(symbol)}({i})"
            for i, symbol in enumerate(FNO_INDEX_SYMBOLS, start=1)
        ) + " [default 1]: ",
        [
            {"label": get_fno_display_name(symbol), "key": i, "value": symbol}
            for i, symbol in enumerate(FNO_INDEX_SYMBOLS, start=1)
        ],
        default=1,
    )


def prompt_fno_expiry_selection(base_symbol, instrument_type):
    expiries = get_available_expiries(base_symbol, instrument_type=instrument_type)
    if not expiries:
        raise RuntimeError(
            f"No active {instrument_type} expiries found for {get_fno_display_name(base_symbol)}."
        )

    log_event(f"[SETUP] Available expiries for {get_fno_display_name(base_symbol)}:")
    for idx, expiry in enumerate(expiries, start=1):
        log_event(f"[SETUP]   {idx}. {expiry}")

    log_event("[SETUP] Choose expiry or press Enter to use the nearest available expiry")
    expiry_choice = prompt_int(
        f"Choose expiry [default 1]: ",
        default=1,
        minimum=1,
        maximum=len(expiries),
    )
    return expiries[expiry_choice - 1]


def prompt_fno_option_contract_selection(base_symbol):
    expiry = prompt_fno_expiry_selection(base_symbol, instrument_type="OPT")
    option_type = prompt_choice(
        "Option type: CE(1) or PE(2)? [default 1]: ",
        [
            {"label": "CE", "key": 1, "value": "CE"},
            {"label": "PE", "key": 2, "value": "PE"},
        ],
        default=1,
    )
    strikes = get_available_option_strikes(base_symbol, expiry, option_type)
    if not strikes:
        raise RuntimeError(
            f"No {option_type} strikes found for {get_fno_display_name(base_symbol)} {expiry}."
        )

    log_event(f"[SETUP] Available strikes for {get_fno_display_name(base_symbol)}:")
    strikes_to_show = strikes[:8] if len(strikes) <= 8 else strikes[:5] + ["..."] + strikes[-3:]
    log_event(f"[SETUP]   {strikes_to_show}")
    log_event("[SETUP] Press Enter to use the ATM-like default strike")

    default_strike = get_atm_option_strike(base_symbol, expiry, option_type)
    while True:
        raw = input(f"Enter strike price [default {default_strike}]: ").strip()
        if not raw:
            strike = default_strike
        else:
            try:
                strike = int(raw)
            except ValueError:
                log_event("[INPUT] Enter a valid strike price.", "warning")
                continue

        if strike in strikes:
            break

        log_event(
            (
                f"[INPUT] Strike {strike} is not available for "
                f"{get_fno_display_name(base_symbol)} {expiry} {option_type}."
            ),
            "warning",
        )

    contract = resolve_option_contract(base_symbol, expiry, strike, option_type)
    log_event(
        f"[MAIN] Resolved F&O option contract for "
        f"{get_fno_display_name(base_symbol)}: {contract}"
    )
    return [contract], "FNO"


def prompt_fno_contract_selection(engine_name):
    selection = prompt_fno_base_symbols(engine_name)
    if engine_name == "futures_equity":
        base_symbols = list(FNO_INDEX_SYMBOLS) if selection == "BOTH" else [selection]
        contracts = []
        for base_symbol in base_symbols:
            expiry = prompt_fno_expiry_selection(base_symbol, instrument_type="FUT")
            contract = resolve_futures_contract(base_symbol, expiry)
            contracts.append(contract)
            log_event(
                f"[MAIN] Resolved F&O futures contract for "
                f"{get_fno_display_name(base_symbol)}: {contract}"
            )
        return contracts, "FNO"

    return prompt_fno_option_contract_selection(selection)


def prompt_multi_strategy_selection(strategy_options):
    log_event("Choose strategies:")
    for key, value in strategy_options.items():
        log_event(f"{key}. {value}")

    while True:
        raw = input("Enter numbers separated by commas: ").strip()
        selected_keys = [item.strip() for item in raw.split(",") if item.strip()]

        if not selected_keys:
            log_event("[INPUT] Select at least one strategy.", "warning")
            continue

        invalid = [key for key in selected_keys if key not in strategy_options]
        if invalid:
            log_event(
                f"[INPUT] Invalid strategy numbers: {', '.join(invalid)}",
                "warning",
            )
            continue

        strategies = []
        seen = set()
        for key in selected_keys:
            strategy = strategy_options[key]
            if strategy not in seen:
                strategies.append(strategy)
                seen.add(strategy)

        log_event(f"[MAIN] Strategies selected: {strategies}")
        return strategies


def log_market_context(symbol, context):
    log_event(
        (
            f"[MARKET] {symbol} | Gap={context['gap_percent']:.2f}% | "
            f"GapType={context['gap_type']} | "
            f"Behavior={context['behavior']} | "
            f"Strategies={context['strategies']} | "
            f"MinConf={context['min_confirmations']} | "
            f"AllowEntries={context['allow_entries']}"
        )
    )
    if context.get("reason"):
        log_event(f"[MARKET] {symbol} | {context['reason']}")


def get_cached_regime_context(regime_cache, symbol, trade_day):
    cached = regime_cache.get(symbol)
    if not cached:
        return None
    if cached.get("trade_day") != trade_day.isoformat():
        return None
    return cached.get("context")


def parse_trade_day(raw_value):
    try:
        return date.fromisoformat(raw_value)
    except (TypeError, ValueError):
        return datetime.now().date()


def save_runtime_state(
    engine_name,
    positions,
    traded_symbols_today,
    active_trade_day,
    last_entry_time,
    regime_cache,
):
    save_engine_state(
        engine_name=engine_name,
        positions=positions,
        traded_symbols_today=traded_symbols_today,
        active_trade_day=active_trade_day,
        last_entry_time=last_entry_time,
        regime_cache=regime_cache,
    )


def log_ranked_candidates(candidates):
    if not candidates:
        log_event("[SCAN] No actionable ranked candidates")
        return

    log_event("[SCAN] Ranked candidates:")
    for index, candidate in enumerate(candidates, start=1):
        log_event(
            (
                f"[SCAN] Rank {index} | {candidate['symbol']} | "
                f"Signal={candidate['signal']} | "
                f"Agree={candidate['agreement_count']} | "
                f"Score={candidate['score']:.4f} | "
                f"ATR={candidate['atr']:.2f} | "
                f"Last close={candidate['latest_close']:.2f}"
            )
        )


def summarize_execution_stats(engine, capital, positions):
    deployed_capital = get_deployed_capital(positions)
    open_count = len(positions)

    log_event("[STATS] Execution summary:")
    log_event(f"[STATS] Starting capital: {capital:.2f}")
    log_event(f"[STATS] Open positions: {open_count}")
    log_event(f"[STATS] Deployed capital (entry exposure): {deployed_capital:.2f}")
    log_event(f"[STATS] Capital reserve estimate: {max(0.0, capital - deployed_capital):.2f}")

    if open_count == 0:
        log_event("[STATS] No open positions to evaluate for unrealized P/L")
        return

    total_market_value = 0.0
    total_unrealized = 0.0
    for symbol, position in positions.items():
        try:
            data = get_data(
                symbol,
                period=engine.data_period,
                interval=engine.data_interval,
            )
        except Exception as exc:
            log_event(
                f"[STATS] Could not fetch latest data for {symbol}: {exc}",
                "warning",
            )
            continue

        if data.empty:
            log_event(f"[STATS] No latest data for {symbol}, skipping P/L calculation", "warning")
            continue

        latest_close = float(data.iloc[-1]["Close"])
        market_value = latest_close * position["quantity"]
        total_market_value += market_value

        if position["side"] == "BUY":
            pnl = (latest_close - position["entry_price"]) * position["quantity"]
        else:
            pnl = (position["entry_price"] - latest_close) * position["quantity"]

        total_unrealized += pnl
        log_event(
            (
                f"[STATS] {symbol} {position['side']} qty={position['quantity']} "
                f"entry={position['entry_price']:.2f} last={latest_close:.2f} "
                f"market_value={market_value:.2f} pnl={pnl:.2f}"
            )
        )

    log_event(f"[STATS] Total market value of open positions: {total_market_value:.2f}")
    log_event(f"[STATS] Total unrealized P/L: {total_unrealized:.2f}")


def force_square_off_positions(engine, positions):
    if not positions:
        return False

    log_event("[SQUAREOFF] Closing all open positions")
    changed = False
    for symbol, position in list(positions.items()):
        exit_side = "SELL" if position["side"] == "BUY" else "BUY"
        place_order(
            exit_side,
            position["quantity"],
            symbol,
            note="Intraday square-off",
            product=engine.order_product,
        )
        del positions[symbol]
        changed = True

    return changed


def manage_open_positions(
    engine,
    positions,
    symbol_snapshots,
):
    state_changed = False
    for symbol, position in list(positions.items()):
        snapshot = symbol_snapshots.get(symbol)

        if not snapshot:
            log_event(
                f"[ERROR] Missing latest data for open symbol {symbol}",
                "error",
            )
            continue

        trailing_updated = update_trailing_stop(
            position,
            snapshot["latest_close"],
            engine.trailing_percent,
        )
        if trailing_updated:
            log_event(
                f"[TRAILING] {symbol} trailing stop updated to "
                f"{position['trailing_stop']:.2f} "
                f"(best_price={position['best_price']:.2f})"
            )
            state_changed = True

        exit_reason = engine.evaluate_position_exit(
            position,
            snapshot["latest_candle"],
        )
        if exit_reason:
            exit_side = "SELL" if position["side"] == "BUY" else "BUY"
            log_event(
                f"[EXIT] {symbol} {exit_reason} triggered at "
                f"{snapshot['latest_close']:.2f}"
            )
            place_order(
                exit_side,
                position["quantity"],
                symbol,
                note=f"Exit {position['side']} via {exit_reason}",
                product=engine.order_product,
            )
            del positions[symbol]
            state_changed = True
            continue

        log_event(
            (
                f"[POSITION] {symbol} no exit triggered, "
                f"SL={position['stop_loss']:.2f}, "
                f"Target={position['target']:.2f}, "
                f"Current={snapshot['latest_close']:.2f}"
            )
        )

        signal_exit_reason = engine.get_signal_exit_reason(
            position,
            snapshot["signal"],
        )
        if signal_exit_reason:
            exit_side = "SELL" if position["side"] == "BUY" else "BUY"
            log_event(
                f"[EXIT] Signal-based exit for {symbol}: "
                f"{snapshot['signal']} ({signal_exit_reason})"
            )
            place_order(
                exit_side,
                position["quantity"],
                symbol,
                note=f"Close {position['side']} via {signal_exit_reason}",
                product=engine.order_product,
            )
            del positions[symbol]
            state_changed = True

    return state_changed


logger = setup_session_logger()
session_log_path = None

try:
    log_event("Starting Algo Bot...\n")
    log_event("[SETUP] Choose your data provider - this determines where market data comes from")
    log_event("[SETUP]   YFINANCE: Free, no authentication needed, good for testing")
    log_event("[SETUP]   KITE: Live data from Zerodha, requires API credentials")
    log_event("[SETUP]   UPSTOX: Live data from Upstox, requires API credentials")

    data_provider = prompt_choice(
        "Data provider: YFINANCE(1), KITE(2), UPSTOX(3)? [default 1]: ",
        [
            {"label": "YFINANCE", "key": 1, "value": "YFINANCE"},
            {"label": "KITE", "key": 2, "value": "KITE"},
            {"label": "UPSTOX", "key": 3, "value": "UPSTOX"},
        ],
        default=1,
    )
    set_data_provider(data_provider)
    log_event(f"[MAIN] Data provider selected: {data_provider}")

    log_event("[SETUP] Choose execution mode - CRITICAL SAFETY SETTING")
    log_event("[SETUP]   PAPER: Simulates trading, NO real orders placed")
    log_event("[SETUP]   LIVE: Places REAL orders with your broker - USE WITH CAUTION")

    execution_mode = prompt_choice(
        "Execution mode: PAPER(1) or LIVE(9)? [default 9]: ",
        [
            {"label": "PAPER", "key": 1, "value": "PAPER"},
            {"label": "LIVE", "key": 9, "value": "LIVE"},
        ],
        default=9,
    )
    set_execution_mode(execution_mode)
    log_event(f"[MAIN] Execution mode selected: {execution_mode}")

    log_event("[SETUP] Choose your broker for order execution")
    log_event("[SETUP]   KITE: Zerodha's trading platform")
    log_event("[SETUP]   UPSTOX: Upstox trading platform")

    execution_provider = prompt_choice(
        "Execution provider: KITE(1) or UPSTOX(2)? [default 1]: ",
        [
            {"label": "KITE", "key": 1, "value": "KITE"},
            {"label": "UPSTOX", "key": 2, "value": "UPSTOX"},
        ],
        default=1,
    )
    set_execution_provider(execution_provider)
    log_event(f"[MAIN] Execution provider selected: {execution_provider}")

    log_event("[SETUP] Choose trading engine - determines trading style and timeframe")
    log_event("[SETUP]   INTRADAY EQUITY: 1-minute data, MIS product, 9:15-15:30, auto square-off")
    log_event("[SETUP]   DELIVERY EQUITY: Daily data, CNC product, long-term holding")
    log_event("[SETUP]   FUTURES EQUITY: Index futures on NIFTY 50 and SENSEX via Kite derivatives")
    log_event("[SETUP]   OPTIONS EQUITY: Index options on NIFTY 50 and SENSEX with ATM strike assist")

    engine_choice = prompt_choice(
        "Engine: INTRADAY EQUITY(1), DELIVERY EQUITY(2), FUTURES EQUITY(3), OPTIONS EQUITY(4)? [default 1]: ",
        [
            {"label": "INTRADAY EQUITY", "key": 1, "value": "1"},
            {"label": "DELIVERY EQUITY", "key": 2, "value": "2"},
            {"label": "FUTURES EQUITY", "key": 3, "value": "3"},
            {"label": "OPTIONS EQUITY", "key": 4, "value": "4"},
        ],
        default=1,
    )

    log_event("[SETUP] Enter your trading capital - this is the maximum amount the bot can risk")
    log_event("[SETUP]   For PAPER mode: Use any amount for simulation")
    log_event("[SETUP]   For LIVE mode: Use amount you're comfortable losing")

    capital = prompt_float("Enter capital for strategy: ", minimum=1)

    if engine_choice in {"3", "4"}:
        selected_symbols, symbol_mode = prompt_fno_contract_selection(
            ENGINE_OPTIONS[engine_choice].name
        )
    else:
        selected_symbols, symbol_mode = prompt_symbol_selection()

    log_event("[SETUP] Choose risk style - affects stop-loss distance and position sizing")
    log_event("[SETUP]   CONSERVATIVE: 1.5x ATR stops, 0.5% risk per trade, safer but fewer trades")
    log_event("[SETUP]   BALANCED: 2.0x ATR stops, 1.0% risk per trade, good balance")
    log_event("[SETUP]   AGGRESSIVE: 2.5x ATR stops, 1.5% risk per trade, higher risk/reward")

    risk_style_key = prompt_choice(
        (
            "Risk style: CONSERVATIVE(1), BALANCED(2), "
            "AGGRESSIVE(3)? [default 2]: "
        ),
        [
            {"label": "CONSERVATIVE", "key": 1, "value": "1"},
            {"label": "BALANCED", "key": 2, "value": "2"},
            {"label": "AGGRESSIVE", "key": 3, "value": "3"},
        ],
        default=2,
    )
    risk_style = RISK_STYLES[risk_style_key]
    atr_stop_multiplier = risk_style["atr_stop_multiplier"]
    trailing_atr_multiplier = risk_style["trailing_atr_multiplier"]
    target_risk_reward = risk_style["target_risk_reward"]
    sl_percent = risk_style["sl_percent"]
    target_percent = risk_style["target_percent"]
    trailing_percent = risk_style["trailing_percent"]
    risk_percent = risk_style["risk_percent"]

    engine = ENGINE_OPTIONS[engine_choice](
        sl_percent=sl_percent,
        target_percent=target_percent,
        trailing_percent=trailing_percent,
    )

    if engine.name == "delivery_equity":
        log_event("[SETUP] Delivery equity settings - for long-term CNC positions")
        log_event("[SETUP]   Max portfolio allocation per symbol: Maximum % of capital per stock")
        log_event("[SETUP]   Example: 25% means no single stock can exceed 25% of your capital")

        max_symbol_allocation = prompt_float(
            "Max portfolio allocation per delivery symbol % [default 25]: ",
            default=25,
            minimum=1,
            maximum=100,
        )
        engine.set_portfolio_rules(max_symbol_allocation / 100)
        log_event(
            (
                "[MAIN] Delivery portfolio rules | "
                f"Max symbol allocation={max_symbol_allocation:.2f}%"
            )
        )

    if engine.name in {"futures_equity", "options_equity"}:
        if data_provider != "KITE":
            log_event(
                "[MAIN] F&O support currently requires KITE data provider. Switching to KITE.",
                "warning",
            )
            set_data_provider("KITE")
            data_provider = "KITE"
        if execution_provider != "KITE":
            log_event(
                "[MAIN] F&O support currently requires KITE execution provider. Switching to KITE.",
                "warning",
            )
            set_execution_provider("KITE")
            execution_provider = "KITE"
        log_event("[SETUP] F&O engines use Kite derivatives contracts and live broker position sync.")
        log_event(
            "[SETUP] Supported F&O underlyings in this build: NIFTY 50 and SENSEX."
        )

    log_event(f"[MAIN] Engine selected: {engine.name}")
    log_event(
        (
            f"[MAIN] Risk style selected: {risk_style['name']} | "
            f"ATR stop={atr_stop_multiplier:.2f}x | "
            f"ATR trail={trailing_atr_multiplier:.2f}x | "
            f"Target RR={target_risk_reward:.2f}x | "
            f"Capital risk={risk_percent * 100:.2f}%"
        )
    )

    log_event("[SETUP] Position limits - control how many concurrent trades")
    log_event("[SETUP]   Max open positions: How many stocks can be traded simultaneously")
    log_event("[SETUP]   Higher = more diversification, but more capital needed")

    max_open_positions = prompt_int(
        "Max open positions [default 1]: ",
        default=1,
        minimum=1,
    )

    log_event("[SETUP] Capital limits per trade - controls individual position size")
    log_event("[SETUP]   Max capital per trade: Maximum amount to risk on any single stock")
    log_event("[SETUP]   Lower = more conservative, higher = larger positions")

    default_max_capital_per_trade = capital / max_open_positions
    max_capital_per_trade = prompt_float(
        (
            "Max capital per trade "
            f"[default {default_max_capital_per_trade:.2f}]: "
        ),
        default=default_max_capital_per_trade,
        minimum=1,
        maximum=capital,
    )

    log_event("[SETUP] Total capital deployment - overall portfolio exposure")
    log_event("[SETUP]   Max capital deployed: Total amount that can be invested across all positions")
    log_event("[SETUP]   Usually set to your total capital amount")

    max_capital_deployed = prompt_float(
        f"Max capital deployed [default {capital:.2f}]: ",
        default=capital,
        minimum=1,
        maximum=capital,
    )

    log_event("[SETUP] Trading frequency - controls how often to trade each stock")
    log_event("[SETUP]   One trade per symbol per day: YES = only 1 trade per stock daily")
    log_event("[SETUP]   One trade per symbol per day: NO = can trade same stock multiple times")

    one_trade_per_symbol_per_day = prompt_choice(
        "One trade per symbol per day? YES(1) or NO(2) [default 1]: ",
        [
            {"label": "YES", "key": 1, "value": "YES"},
            {"label": "NO", "key": 2, "value": "NO"},
        ],
        default=1,
    ) == "YES"
    
    # Auto-select TOP1 for SINGLE mode (only 1 symbol, so TOP N is irrelevant)
    if symbol_mode == "SINGLE":
        entry_selection_mode = "TOP1"
        top_n_count = 1
        log_event("[MAIN] Single mode detected - entry selection auto-set to TOP 1")
    else:
        log_event("[SETUP] Entry selection - how many top-ranked candidates to trade")
        log_event("[SETUP]   TOP 1: Only enter the highest-ranked signal")
        log_event("[SETUP]   TOP N: Enter the top N highest-ranked signals")

        entry_selection_mode = prompt_choice(
            "Entry selection: TOP 1(1) or TOP N(2)? [default 2]: ",
            [
                {"label": "TOP 1", "key": 1, "value": "TOP1"},
                {"label": "TOP N", "key": 2, "value": "TOPN"},
            ],
            default=2,
        )
        top_n_count = 1
        if entry_selection_mode == "TOPN":
            default_top_n = min(5, max_open_positions)
            top_n_count = prompt_int(
                f"Enter N for TOP N entries [default {default_top_n}]: ",
                default=default_top_n,
                minimum=1,
                maximum=max_open_positions,
            )

    log_event("[SETUP] Strategy mode - how the bot generates trading signals")
    log_event("[SETUP]   Single: Use one specific strategy (MA, RSI, BREAKOUT, VWAP, ORB)")
    log_event("[SETUP]   Multi: Use multiple strategies with agreement confirmation")
    log_event("[SETUP]   Auto Adaptive: Automatically choose strategy based on market conditions")

    if engine.name == "intraday_equity":
        mode_prompt = "Select Mode: 1 (Single) / 2 (Multi) / 3 (Auto Adaptive) [default 3]: "
        default_mode = "3"
    else:
        mode_prompt = "Select Mode: 1 (Single) / 2 (Multi) [default 1]: "
        default_mode = "1"
    mode = input(mode_prompt).strip()
    
    if not mode:
        mode = default_mode
        if engine.name == "intraday_equity" and default_mode == "3":
            log_event("[MAIN] Using Auto Adaptive strategy as default for intraday_equity")

    if mode == "1":
        choices = [
            {"label": value, "key": key, "value": value}
            for key, value in engine.supported_strategies.items()
        ]
        strategy_name = prompt_choice(
            "Choose strategy: ",
            choices,
        )
        log_event(f"[MAIN] Strategy selected: {strategy_name}")
        min_confirmations = None
        strategies = None

    elif mode == "2":
        strategies = prompt_multi_strategy_selection(engine.supported_strategies)
        strategy_count = len(strategies)
        min_confirmations = DEFAULT_CONFIRMATIONS.get(
            strategy_count,
            strategy_count,
        )
        strategy_name = None
        log_event(
            (
                f"[MAIN] Minimum confirmations set to "
                f"{min_confirmations} for {strategy_count} strategies"
            )
        )

    elif mode == "3" and engine.name == "intraday_equity":
        strategy_name = None
        strategies = None
        min_confirmations = None
        log_event("[MAIN] Strategy mode selected: AUTO ADAPTIVE")
        log_event("[MAIN] Auto Adaptive mode will dynamically select strategies based on market conditions")
        log_event("[MAIN]   - Gap Up: Uses ORB strategy")
        log_event("[MAIN]   - Gap Down: Uses RSI/BREAKOUT strategy")
        log_event("[MAIN]   - Normal: Uses MA strategy with VWAP bias")

    else:
        log_event("Invalid mode. Exiting.", "error")
        raise SystemExit

    log_event(
        (
            f"[MAIN] Scan configuration | Engine={engine.name} | "
            f"Data provider={data_provider} | "
            f"Execution provider={execution_provider} | "
            f"Symbol mode={symbol_mode} | Symbols={len(selected_symbols)} | "
            f"Data={engine.data_period}/{engine.data_interval} | "
            f"Mode={mode} | "
            f"Max positions={max_open_positions} | "
            f"Max/trade={max_capital_per_trade:.2f} | "
            f"Max deployed={max_capital_deployed:.2f} | "
            f"One trade/day={one_trade_per_symbol_per_day} | "
            f"Selection={entry_selection_mode} | "
            f"Top N={top_n_count}"
        )
    )

    saved_state = load_engine_state(engine.name)
    positions = engine.reconcile_startup(
        execution_mode=execution_mode,
        persisted_positions=saved_state["positions"],
    )
    traded_symbols_today = set(saved_state["traded_symbols_today"])
    active_trade_day = parse_trade_day(saved_state["active_trade_day"])
    last_entry_time = float(saved_state["last_entry_time"])
    regime_cache = saved_state["regime_cache"]
    save_runtime_state(
        engine.name,
        positions,
        traded_symbols_today,
        active_trade_day,
        last_entry_time,
        regime_cache,
    )

    while True:
        now = datetime.now()
        current_trade_day = now.date()
        if current_trade_day != active_trade_day:
            active_trade_day = current_trade_day
            traded_symbols_today.clear()
            log_event("[MAIN] New day detected, reset traded symbol tracker")
            if engine.name == "intraday_equity" and positions:
                log_event(
                    "[MAIN] Clearing stale intraday positions for new day",
                    "warning",
                )
                positions.clear()
            regime_cache = {}
            save_runtime_state(
                engine.name,
                positions,
                traded_symbols_today,
                active_trade_day,
                last_entry_time,
                regime_cache,
            )

        cycle_state = engine.get_cycle_state(now)
        log_event("\n==============================")
        log_event("New Cycle Started")
        log_event("==============================")
        log_event(f"[SESSION] {cycle_state['reason']}")

        if cycle_state["force_square_off"]:
            if force_square_off_positions(engine, positions):
                save_runtime_state(
                    engine.name,
                    positions,
                    traded_symbols_today,
                    active_trade_day,
                    last_entry_time,
                    regime_cache,
                )
            log_positions(positions, log_event)
            time.sleep(engine.sleep_seconds)
            continue

        if not cycle_state["allow_scan"] and not (
            cycle_state["manage_positions"] and positions
        ):
            log_positions(positions, log_event)
            time.sleep(engine.sleep_seconds)
            continue

        current_time = time.time()
        symbol_snapshots = {}
        candidates = []
        symbols_to_refresh = list(
            dict.fromkeys(selected_symbols + list(positions.keys()))
        )

        for symbol in symbols_to_refresh:
            data = get_data(
                symbol,
                period=engine.data_period,
                interval=engine.data_interval,
            )

            if data.empty:
                log_event(f"[ERROR] No data for {symbol}", "error")
                continue

            latest_candle = data.iloc[-1]
            latest_close = float(latest_candle["Close"])
            active_mode = mode
            active_strategy_name = strategy_name
            active_strategies = strategies
            active_min_confirmations = min_confirmations
            market_context = None
            intraday_history = None

            if (
                engine.name == "intraday_equity"
                and engine.requires_extended_intraday_history(
                    mode,
                    strategy_name=strategy_name,
                    strategies=strategies,
                )
            ):
                intraday_history = get_data(
                    symbol,
                    period="5d",
                    interval="1m",
                )

            if mode == "3" and engine.name == "intraday_equity":
                market_context = get_cached_regime_context(
                    regime_cache,
                    symbol,
                    current_trade_day,
                )
                if market_context is None:
                    daily_data = get_data(
                        symbol,
                        period="5d",
                        interval="1d",
                    )
                    market_context = engine.build_market_context(
                        symbol,
                        data,
                        daily_data,
                    )
                    if market_context.get("cacheable"):
                        regime_cache[symbol] = {
                            "trade_day": current_trade_day.isoformat(),
                            "context": market_context,
                        }
                        save_runtime_state(
                            engine.name,
                            positions,
                            traded_symbols_today,
                            active_trade_day,
                            last_entry_time,
                            regime_cache,
                        )
                log_market_context(symbol, market_context)
                active_mode = "2"
                active_strategy_name = None
                active_strategies = market_context["strategies"]
                active_min_confirmations = market_context["min_confirmations"]

            evaluation = evaluate_symbol_signal(
                data,
                active_mode,
                strategy_name=active_strategy_name,
                strategies=active_strategies,
                min_confirmations=active_min_confirmations,
            )
            if engine.name == "intraday_equity":
                evaluation = engine.apply_signal_filters(
                    evaluation,
                    data,
                    intraday_history_df=intraday_history,
                    min_confirmations=active_min_confirmations or 1,
                )

            symbol_snapshots[symbol] = {
                "data": data,
                "latest_candle": latest_candle,
                "latest_close": latest_close,
                "signal": evaluation["signal"],
                "agreement_count": evaluation["agreement_count"],
                "score": evaluation["score"],
                "details": evaluation["details"],
                "market_context": market_context,
                "vwap_bias": evaluation.get("vwap_bias"),
                "breakout_volume_note": evaluation.get("breakout_volume_note"),
                "atr": get_atr_value(data),
            }

            log_event(
                (
                    f"[SCAN] {symbol} | Signal={evaluation['signal']} | "
                    f"Agree={evaluation['agreement_count']} | "
                    f"Score={evaluation['score']:.4f} | "
                    f"ATR={symbol_snapshots[symbol]['atr']:.2f} | "
                    f"Last close={latest_close:.2f} | "
                    f"VWAP bias={evaluation.get('vwap_bias', 'N/A')}"
                )
            )
            if evaluation.get("breakout_volume_note"):
                log_event(
                    f"[SCAN] {symbol} | Breakout volume filter: "
                    f"{evaluation['breakout_volume_note']}"
                )

            allow_symbol_entries = True
            if market_context is not None:
                allow_symbol_entries = market_context["allow_entries"]

            normalized_signal = engine.normalize_entry_signal(evaluation["signal"])
            if normalized_signal and not allow_symbol_entries:
                log_event(
                    f"[LIMIT] {symbol} adaptive mode not ready for entries yet"
                )
                normalized_signal = None
            if normalized_signal:
                candidates.append(
                    {
                        "symbol": symbol,
                        "signal": normalized_signal,
                        "agreement_count": evaluation["agreement_count"],
                        "score": evaluation["score"],
                        "latest_close": latest_close,
                        "atr": symbol_snapshots[symbol]["atr"],
                    }
                )

        if not symbol_snapshots and positions:
            log_event("[ERROR] No symbol data available for open positions", "error")
        elif not symbol_snapshots:
            log_event("[ERROR] No symbol data available in this cycle", "error")
            time.sleep(engine.sleep_seconds)
            continue

        ranked_candidates = rank_candidates(candidates)
        log_ranked_candidates(ranked_candidates)

        state_changed = False
        if cycle_state["manage_positions"]:
            state_changed = manage_open_positions(
                engine,
                positions,
                symbol_snapshots,
            )
            if state_changed:
                save_runtime_state(
                    engine.name,
                    positions,
                    traded_symbols_today,
                    active_trade_day,
                    last_entry_time,
                    regime_cache,
                )

        deployed_capital = get_deployed_capital(positions)
        log_event(f"[RISK] Current deployed capital: {deployed_capital:.2f}")
        cooldown_active = (
            engine.cooldown_seconds > 0
            and current_time - last_entry_time < engine.cooldown_seconds
        )

        if not cycle_state["allow_entries"]:
            log_event("[SESSION] New entries disabled in current window")
        elif cooldown_active:
            log_event("[COOLDOWN] Skipping new entries")
        else:
            planned_entries = (
                ranked_candidates[:1]
                if entry_selection_mode == "TOP1"
                else ranked_candidates[:top_n_count]
            )

            for candidate in planned_entries:
                symbol = candidate["symbol"]

                if symbol in positions:
                    log_event(f"[LIMIT] {symbol} already has an open position")
                    continue

                if one_trade_per_symbol_per_day and symbol in traded_symbols_today:
                    log_event(
                        f"[LIMIT] {symbol} already traded today, skipping"
                    )
                    continue

                if len(positions) >= max_open_positions:
                    log_event("[LIMIT] Max open positions reached")
                    break

                entry_price = candidate["latest_close"]
                atr_value = candidate.get("atr", 0.0)
                stop_data = atr_stop_from_value(
                    candidate["signal"],
                    entry_price,
                    atr_value,
                    atr_stop_multiplier,
                )
                if stop_data["stop_distance"] <= 0:
                    log_event(
                        f"[RISK] ATR unavailable for {symbol}, skipping entry",
                        "warning",
                    )
                    continue

                sizing = atr_position_size(
                    capital=capital,
                    entry_price=entry_price,
                    atr_value=atr_value,
                    atr_multiplier=atr_stop_multiplier,
                    risk_percent=risk_percent,
                )
                qty = sizing["quantity"]
                qty = apply_capital_limits_to_quantity(
                    qty,
                    entry_price,
                    max_capital_per_trade,
                    max_capital_deployed,
                    deployed_capital,
                    log_event,
                )
                qty = engine.apply_entry_allocation_limit(
                    symbol,
                    qty,
                    entry_price,
                    positions,
                    capital,
                )

                if qty <= 0:
                    log_event(
                        (
                            f"[RISK] Quantity is 0 for {symbol} after applying "
                            "risk and capital limits, skipping"
                        ),
                        "warning",
                    )
                    continue

                estimated_trade_capital = entry_price * qty
                target_distance = stop_data["stop_distance"] * target_risk_reward
                trailing_distance = atr_value * trailing_atr_multiplier
                target_price = calculate_target_price(
                    candidate["signal"],
                    entry_price,
                    target_distance,
                )
                trailing_stop = (
                    entry_price - trailing_distance
                    if candidate["signal"] == "BUY"
                    else entry_price + trailing_distance
                )

                log_event(
                    (
                        f"[ENTRY] Executing trade on {symbol} | "
                        f"Signal={candidate['signal']} | "
                        f"Agree={candidate['agreement_count']} | "
                        f"Score={candidate['score']:.4f} | "
                        f"ATR={atr_value:.2f} | "
                        f"Stop={stop_data['stop_loss_price']:.2f} | "
                        f"Qty={qty}"
                    )
                )
                place_order(
                    candidate["signal"],
                    qty,
                    symbol,
                    note="Entry",
                    product=engine.order_product,
                )
                positions[symbol] = build_position(
                    symbol=symbol,
                    side=candidate["signal"],
                    quantity=qty,
                    entry_price=entry_price,
                    stop_loss=stop_data["stop_loss_price"],
                    target=target_price,
                    trailing_stop=trailing_stop,
                    trailing_distance=trailing_distance,
                    atr=atr_value,
                    stop_distance=stop_data["stop_distance"],
                )
                traded_symbols_today.add(symbol)
                deployed_capital += estimated_trade_capital
                last_entry_time = current_time
                log_event(f"[RISK] Updated deployed capital: {deployed_capital:.2f}")
                save_runtime_state(
                    engine.name,
                    positions,
                    traded_symbols_today,
                    active_trade_day,
                    last_entry_time,
                    regime_cache,
                )

                if entry_selection_mode == "TOP1":
                    break

        if not ranked_candidates:
            log_event("[MAIN] No new trade")

        log_positions(positions, log_event)
        time.sleep(engine.sleep_seconds)

except KeyboardInterrupt:
    log_event("\n[MAIN] Bot stopped by user.")
except Exception as exc:
    log_event(f"\n[ERROR] {exc}", "error")
    logger.exception("[MAIN] Unhandled exception")
    raise
finally:
    if "positions" in locals() and "engine" in locals() and "capital" in locals():
        try:
            summarize_execution_stats(engine, capital, positions)
        except Exception as exc:
            log_event(f"[STATS] Failed to generate summary: {exc}", "warning")

    session_log_path = finalize_session_logger()
    if session_log_path:
        print(f"[LOG] Session log saved to {session_log_path}")
