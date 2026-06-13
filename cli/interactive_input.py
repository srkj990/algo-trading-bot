from __future__ import annotations

from config import MANUAL_SYMBOL_TABLE, NIFTY50_SYMBOLS, SINGLE_SYMBOL_TABLE, is_intraday_options_engine_name
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
    default_keys = [
        key
        for key, value in strategy_options.items()
        if value in {"MA", "RSI", "BREAKOUT"}
    ]
    default_text = ",".join(default_keys)
    log_help(
        "Enter one or more strategy numbers separated by commas. "
        f"Default: {default_text} for MA, RSI, BREAKOUT"
    )

    while True:
        raw = input(f"Enter numbers separated by commas [default {default_text}]: ").strip()
        selected_keys = [item.strip() for item in raw.split(",") if item.strip()]
        if not selected_keys and default_keys:
            selected_keys = default_keys

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


_IOPTS_STRATEGIES = {
    "1": "ATM_MULTI",
    "2": "ATM_ORB",
    "3": "ATM_BREAKOUT_EXPANSION",
    "4": "ATM_TRAP_REVERSAL",
    "5": "ATM_MOMENTUM",
    "6": "ATM_IV_EXPANSION",
    "7": "ATM_VWAP_REVERSION",
}
_IOPTS_HINTS = {
    "ATM_MULTI": "best all-round",
    "ATM_ORB": "morning directional",
    "ATM_BREAKOUT_EXPANSION": "huge trending moves",
    "ATM_TRAP_REVERSAL": "fake breakout reversals",
    "ATM_MOMENTUM": "trend continuation",
    "ATM_IV_EXPANSION": "volatility ignition",
    "ATM_VWAP_REVERSION": "sideways only",
}


def prompt_strategy_configuration(engine, default_confirmations):
    if is_intraday_options_engine_name(engine.name):
        log_event("[SETUP] Intraday options — choose Single (one strategy) or Multi (combine, N must agree)")
        log_help("Single: pick one ATM strategy. Multi: pick several — entry fires only when N agree. Example: 1 for Single")
        iopts_mode = prompt_choice(
            "Strategy mode: Single(1) or Multi(2) [default 1]: ",
            [
                {"label": "SINGLE", "key": 1, "value": "1"},
                {"label": "MULTI",  "key": 2, "value": "2"},
            ],
            default=1,
        )
        if iopts_mode == "1":
            log_help("Ranked best→situational. Example: 1 for ATM_MULTI (best all-round)")
            log_event("[SETUP] Available intraday options strategies (ranked):")
            for k, v in _IOPTS_STRATEGIES.items():
                log_event(f"[SETUP]   {k}. {v}  ({_IOPTS_HINTS.get(v, '')})")
            strategy_name = prompt_choice(
                "Choose strategy [default 1]: ",
                [{"label": v, "key": int(k), "value": v} for k, v in _IOPTS_STRATEGIES.items()],
                default=1,
            )
            log_event(f"[MAIN] Intraday options strategy selected: {strategy_name}")
            return "1", strategy_name, None, None
        else:
            log_event("[SETUP] Available intraday options strategies (ranked):")
            for k, v in _IOPTS_STRATEGIES.items():
                log_event(f"[SETUP]   {k}. {v}  ({_IOPTS_HINTS.get(v, '')})")
            strategies = prompt_multi_strategy_selection(_IOPTS_STRATEGIES)
            strategy_count = len(strategies)
            log_help(
                "How many selected strategies must agree before entry fires. "
                "2 = balanced (recommended), 1 = any fires, 3+ = high-conviction only. Example: 2"
            )
            min_confirmations = prompt_int(
                f"Minimum confirmations [default 1]: ",
                default=1,
                minimum=1,
                maximum=strategy_count,
            )
            log_event(f"[MAIN] Intraday options multi-strategy: {', '.join(strategies)} | min_confirmations={min_confirmations}")
            return "2", None, strategies, min_confirmations

    if engine.name == "intraday_equity":
        log_event("[SETUP] Strategy mode - choose how intraday equity signals are generated")
        log_help("Choose whether intraday equity should use one strategy, multiple strategies, or adaptive selection. Example: 2 for MULTI")
        mode = prompt_choice(
            "Strategy mode: SINGLE(1), MULTI(2), AUTO ADAPTIVE(3) [default 2]: ",
            [
                {"label": "SINGLE", "key": 1, "value": "1"},
                {"label": "MULTI", "key": 2, "value": "2"},
                {"label": "AUTO ADAPTIVE", "key": 3, "value": "3"},
            ],
            default=2,
        )
        if mode == "3":
            log_event("[MAIN] Strategy mode selected: AUTO ADAPTIVE")
            return mode, None, None, None
    else:
        log_event("[SETUP] Strategy mode - choose how entries are generated for this engine")
        log_help("Choose whether this engine should use one strategy or combine multiple strategies. Example: 2 for MULTI")
        mode = prompt_choice(
            "Strategy mode: SINGLE(1) or MULTI(2) [default 2]: ",
            [
                {"label": "SINGLE", "key": 1, "value": "1"},
                {"label": "MULTI", "key": 2, "value": "2"},
            ],
            default=2,
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
