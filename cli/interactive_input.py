from __future__ import annotations

from config import MANUAL_SYMBOL_TABLE, NIFTY50_SYMBOLS, SINGLE_SYMBOL_TABLE
from logger import log_event


def log_help(message: str) -> None:
    log_event(f"[HELP] {message}")


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
    normalized = {str(choice["key"]): choice["value"].upper() for choice in valid_choices}
    display = ", ".join(f"{choice['label']}:{choice['key']}" for choice in valid_choices)
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
    log_help("Choose whether to scan one symbol, a custom shortlist, or the full NIFTY50 universe. Example: 1 for SINGLE")

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
            log_help("Choose a symbol number from the single-symbol table. Example: 11 for RPOWER.NS")

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
            log_help("Choose one or more table numbers separated by commas. Example: 11,12,13")

            while True:
                raw = input("Enter table numbers separated by commas: ").strip()
                selected_keys = [item.strip() for item in raw.split(",") if item.strip()]
                if not selected_keys:
                    log_event("[INPUT] Select at least one symbol.", "warning")
                    continue

                invalid = [key for key in selected_keys if key not in MANUAL_SYMBOL_TABLE]
                if invalid:
                    log_event(f"[INPUT] Invalid table numbers: {', '.join(invalid)}", "warning")
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
            raw = input("Enter symbols separated by commas (example: RELIANCE,INFY,TCS): ").strip()
            selected = [normalize_symbol(item) for item in raw.split(",") if item.strip()]
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

    log_event(f"[MAIN] Using NIFTY50 universe with {len(NIFTY50_SYMBOLS)} symbols")
    return list(NIFTY50_SYMBOLS), symbol_mode


def prompt_multi_strategy_selection(strategy_options):
    log_event("Choose strategies:")
    for key, value in strategy_options.items():
        log_event(f"{key}. {value}")
    log_help("Enter one or more strategy numbers separated by commas. Example: 1,3,5")

    while True:
        raw = input("Enter numbers separated by commas: ").strip()
        selected_keys = [item.strip() for item in raw.split(",") if item.strip()]

        if not selected_keys:
            log_event("[INPUT] Select at least one strategy.", "warning")
            continue

        invalid = [key for key in selected_keys if key not in strategy_options]
        if invalid:
            log_event(f"[INPUT] Invalid strategy numbers: {', '.join(invalid)}", "warning")
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


def prompt_strategy_configuration(engine, default_confirmations):
    if engine.name == "intraday_options":
        log_event("[SETUP] Intraday options strategy selection - choose the ATM options setup")
        log_help("Choose the intraday options strategy to drive ATM option entries. Example: 3 for VWAP Reversion")
        strategy_name = prompt_choice(
            (
                "Intraday options strategy: Momentum(1), ORB(2), "
                "VWAP Reversion(3), Multi-strategy(4), Breakout Expansion(5), "
                "IV Expansion(6), Trap Reversal(7) [default 1]: "
            ),
            [
                {"label": "MOMENTUM", "key": 1, "value": "ATM_MOMENTUM"},
                {"label": "ORB", "key": 2, "value": "ATM_ORB"},
                {"label": "VWAP REVERSION", "key": 3, "value": "ATM_VWAP_REVERSION"},
                {"label": "MULTI-STRATEGY", "key": 4, "value": "ATM_MULTI"},
                {"label": "BREAKOUT EXPANSION", "key": 5, "value": "ATM_BREAKOUT_EXPANSION"},
                {"label": "IV EXPANSION", "key": 6, "value": "ATM_IV_EXPANSION"},
                {"label": "TRAP REVERSAL", "key": 7, "value": "ATM_TRAP_REVERSAL"},
            ],
            default=1,
        )
        log_event(f"[MAIN] Intraday options strategy selected: {strategy_name}")
        return "1", strategy_name, None, None

    if engine.name == "intraday_equity":
        log_event("[SETUP] Strategy mode - choose how intraday equity signals are generated")
        log_help("Choose whether intraday equity should use one strategy, multiple strategies, or adaptive selection. Example: 3 for AUTO ADAPTIVE")
        mode = prompt_choice(
            "Strategy mode: SINGLE(1), MULTI(2), AUTO ADAPTIVE(3) [default 3]: ",
            [
                {"label": "SINGLE", "key": 1, "value": "1"},
                {"label": "MULTI", "key": 2, "value": "2"},
                {"label": "AUTO ADAPTIVE", "key": 3, "value": "3"},
            ],
            default=3,
        )
        if mode == "3":
            log_event("[MAIN] Strategy mode selected: AUTO ADAPTIVE")
            return mode, None, None, None
    else:
        log_event("[SETUP] Strategy mode - choose how entries are generated for this engine")
        log_help("Choose whether this engine should use one strategy or combine multiple strategies. Example: 1 for SINGLE")
        mode = prompt_choice(
            "Strategy mode: SINGLE(1) or MULTI(2) [default 1]: ",
            [
                {"label": "SINGLE", "key": 1, "value": "1"},
                {"label": "MULTI", "key": 2, "value": "2"},
            ],
            default=1,
        )

    if mode == "1":
        choices = [{"label": value, "key": key, "value": value} for key, value in engine.supported_strategies.items()]
        log_help("Choose one strategy number from the list for this engine. Example: 1")
        strategy_name = prompt_choice("Choose strategy: ", choices)
        log_event(f"[MAIN] Strategy selected: {strategy_name}")
        return mode, strategy_name, None, None

    strategies = prompt_multi_strategy_selection(engine.supported_strategies)
    strategy_count = len(strategies)
    min_confirmations = default_confirmations.get(strategy_count, strategy_count)
    log_event(f"[MAIN] Minimum confirmations set to {min_confirmations} for {strategy_count} strategies")
    return mode, None, strategies, min_confirmations
