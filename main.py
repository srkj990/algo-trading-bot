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
    IntradayFuturesEngine,
    IntradayEquityEngine,
    IntradayOptionsEngine,
    OptionsEquityEngine,
)
from fno_data_fetcher import (
    FNO_INDEX_SYMBOLS,
    get_atm_option_strike,
    get_available_expiries,
    get_available_option_strikes,
    get_contract_lot_size,
    get_fno_display_name,
    get_fno_spot_quote_symbol,
    get_option_greeks_snapshot,
    resolve_futures_contract,
    resolve_option_contract,
)
from engines.common import (
    apply_capital_limits_to_quantity,
    build_position,
    count_open_structures,
    get_deployed_capital,
    log_positions,
    update_trailing_stop,
)
from executor import place_order, set_execution_mode, set_execution_provider
from logger import finalize_session_logger, log_event, setup_session_logger
from risk_manager import (
    atr_position_size,
    atr_stop_from_value,
    calculate_stop_loss_price,
    calculate_target_price,
    position_size,
)
from signal_scoring import (
    evaluate_symbol_signal,
    get_atr_value,
    rank_candidates,
)
from state_store import load_engine_state, save_engine_state
from config import (
    COST_EDGE_BUFFER_RUPEES,
    EXPECTED_EDGE_SCORE_MULTIPLIER,
    MIN_EDGE_TO_COST_RATIO,
    TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER,
    TRANSACTION_COST_MODEL_ENABLED,
    TRANSACTION_SLIPPAGE_PCT_PER_SIDE,
)
from reporting import export_trade_book_report, summarize_by_exit_reason
from transaction_costs import estimate_intraday_equity_round_trip_cost

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
    "5": IntradayFuturesEngine,
    "6": IntradayOptionsEngine,
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

    if "futures" in engine_name:
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


def prompt_option_strike_value(base_symbol, expiry, option_type, label):
    strikes = get_available_option_strikes(base_symbol, expiry, option_type)
    if not strikes:
        raise RuntimeError(
            f"No {option_type} strikes found for {get_fno_display_name(base_symbol)} {expiry}."
        )

    default_strike = get_atm_option_strike(base_symbol, expiry, option_type)
    log_event(
        f"[SETUP] {label} strikes for {get_fno_display_name(base_symbol)} {option_type}:"
    )
    strikes_to_show = strikes[:8] if len(strikes) <= 8 else strikes[:5] + ["..."] + strikes[-3:]
    log_event(f"[SETUP]   {strikes_to_show}")
    log_event(f"[SETUP] ATM reference strike: {default_strike}")

    while True:
        raw = input(f"Enter {label} strike [default {default_strike}]: ").strip()
        if not raw:
            strike = default_strike
        else:
            try:
                strike = int(raw)
            except ValueError:
                log_event("[INPUT] Enter a valid strike price.", "warning")
                continue

        if strike in strikes:
            return strike

        log_event(
            (
                f"[INPUT] Strike {strike} is not available for "
                f"{get_fno_display_name(base_symbol)} {expiry} {option_type}."
            ),
            "warning",
        )


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

    default_strike = get_atm_option_strike(base_symbol, expiry, option_type)
    log_event(f"[SETUP] Available strikes for {get_fno_display_name(base_symbol)}:")
    strikes_to_show = strikes[:8] if len(strikes) <= 8 else strikes[:5] + ["..."] + strikes[-3:]
    log_event(f"[SETUP]   {strikes_to_show}")
    log_event(f"[SETUP] ATM reference strike: {default_strike}")

    strike_mode = prompt_choice(
        "Strike selection: ATM(1), OTM offset(2), ITM offset(3), MANUAL(4)? [default 1]: ",
        [
            {"label": "ATM", "key": 1, "value": "ATM"},
            {"label": "OTM OFFSET", "key": 2, "value": "OTM"},
            {"label": "ITM OFFSET", "key": 3, "value": "ITM"},
            {"label": "MANUAL", "key": 4, "value": "MANUAL"},
        ],
        default=1,
    )

    if strike_mode in {"OTM", "ITM"}:
        atm_index = strikes.index(default_strike)
        step_count = prompt_int(
            "Number of strike steps from ATM [default 1]: ",
            default=1,
            minimum=1,
        )
        direction = 1 if strike_mode == "OTM" else -1
        if option_type == "PE":
            direction *= -1
        selected_index = max(0, min(len(strikes) - 1, atm_index + (direction * step_count)))
        strike = strikes[selected_index]
        log_event(
            f"[MAIN] Selected {strike_mode} strike {strike} from ATM {default_strike}"
        )
    elif strike_mode == "ATM":
        strike = default_strike
        log_event(f"[MAIN] Selected ATM strike {strike}")
    else:
        log_event("[SETUP] Press Enter to use the ATM-like default strike")
        strike = default_strike
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
    lot_size = get_contract_lot_size(contract)
    log_event(
        f"[MAIN] Resolved F&O option contract for "
        f"{get_fno_display_name(base_symbol)}: {contract} | Lot size={lot_size}"
    )
    return [contract], "FNO"


def prompt_intraday_atm_option_selection(base_symbol):
    expiry = prompt_fno_expiry_selection(base_symbol, instrument_type="OPT")
    strike_offset_mode = prompt_choice(
        "ATM strike mode: ATM(1), ATM + 1 STRIKE(2), ATM - 1 STRIKE(3) [default 1]: ",
        [
            {"label": "ATM", "key": 1, "value": "ATM"},
            {"label": "ATM + 1", "key": 2, "value": "ATM_PLUS_1"},
            {"label": "ATM - 1", "key": 3, "value": "ATM_MINUS_1"},
        ],
        default=1,
    )
    strike_offset = {
        "ATM": 0,
        "ATM_PLUS_1": 1,
        "ATM_MINUS_1": -1,
    }[strike_offset_mode]
    scan_symbol = get_fno_spot_quote_symbol(base_symbol)
    log_event(
        (
            f"[MAIN] Intraday ATM options mode selected for {get_fno_display_name(base_symbol)} "
            f"| Expiry={expiry} | Underlying scan symbol={scan_symbol} | "
            f"Strike mode={strike_offset_mode.replace('_', ' ')}"
        )
    )
    atm_option_config = {
        "mode": "ATM_DYNAMIC",
        "underlying": base_symbol,
        "expiry": expiry,
        "scan_symbol": scan_symbol,
        "strike_offset_mode": strike_offset_mode,
        "strike_offset": strike_offset,
    }
    return [scan_symbol], "FNO_ATM", atm_option_config


def prompt_fno_option_pair_selection(base_symbol):
    expiry = prompt_fno_expiry_selection(base_symbol, instrument_type="OPT")
    lower_pe_strike = prompt_option_strike_value(
        base_symbol,
        expiry,
        "PE",
        label="Lower PE",
    )
    upper_ce_strike = prompt_option_strike_value(
        base_symbol,
        expiry,
        "CE",
        label="Upper CE",
    )
    if lower_pe_strike >= upper_ce_strike:
        raise RuntimeError(
            "For a bounded-range pair, the PE strike must be below the CE strike."
        )

    pe_contract = resolve_option_contract(base_symbol, expiry, lower_pe_strike, "PE")
    ce_contract = resolve_option_contract(base_symbol, expiry, upper_ce_strike, "CE")
    pe_lot_size = get_contract_lot_size(pe_contract)
    ce_lot_size = get_contract_lot_size(ce_contract)
    if pe_lot_size != ce_lot_size:
        raise RuntimeError(
            f"Mismatched lot sizes for pair contracts: {pe_lot_size} vs {ce_lot_size}"
        )

    pair_id = f"PAIR:{base_symbol}:{expiry}:{lower_pe_strike}:{upper_ce_strike}"
    log_event(
        f"[MAIN] Resolved two-leg range pair: {pe_contract} + {ce_contract} | Lot size={pe_lot_size}"
    )
    pair_config = {
        "mode": "TWO_LEG_RANGE",
        "pair_id": pair_id,
        "underlying": base_symbol,
        "expiry": expiry,
        "lower_strike": lower_pe_strike,
        "upper_strike": upper_ce_strike,
        "pe_symbol": pe_contract,
        "ce_symbol": ce_contract,
        "symbols": [pe_contract, ce_contract],
        "entry_side": "SELL",
    }
    return [pe_contract, ce_contract], "FNO_PAIR", pair_config


def prompt_fno_contract_selection(engine_name):
    selection = prompt_fno_base_symbols(engine_name)
    if "futures" in engine_name:
        base_symbols = list(FNO_INDEX_SYMBOLS) if selection == "BOTH" else [selection]
        contracts = []
        for base_symbol in base_symbols:
            expiry = prompt_fno_expiry_selection(base_symbol, instrument_type="FUT")
            contract = resolve_futures_contract(base_symbol, expiry)
            lot_size = get_contract_lot_size(contract)
            contracts.append(contract)
            log_event(
                f"[MAIN] Resolved F&O futures contract for "
                f"{get_fno_display_name(base_symbol)}: {contract} | Lot size={lot_size}"
            )
        return contracts, "FNO", None, None

    if engine_name == "intraday_options":
        structure_mode = prompt_choice(
            "Options structure: ATM SINGLE OPTION(1) or TWO-LEG RANGE PAIR(2)? [default 1]: ",
            [
                {"label": "ATM SINGLE OPTION", "key": 1, "value": "SINGLE"},
                {"label": "TWO-LEG RANGE PAIR", "key": 2, "value": "PAIR"},
            ],
            default=1,
        )
        if structure_mode == "PAIR":
            symbols, symbol_mode, pair_config = prompt_fno_option_pair_selection(selection)
            return symbols, symbol_mode, pair_config, None
        symbols, symbol_mode, atm_option_config = prompt_intraday_atm_option_selection(selection)
        return symbols, symbol_mode, None, atm_option_config

    symbols, symbol_mode = prompt_fno_option_contract_selection(selection)
    return symbols, symbol_mode, None, None


def log_selected_fno_contract_summary(
    engine_name,
    selected_symbols,
    option_pair_config=None,
    atm_option_config=None,
):
    if not selected_symbols:
        return

    log_event("[SETUP] Selected F&O contract summary:")
    for symbol in selected_symbols:
        if atm_option_config and symbol == atm_option_config["scan_symbol"]:
            line = (
                f"[SETUP]   ATM SCALP | Underlying={atm_option_config['underlying']} "
                f"| Expiry={atm_option_config['expiry']} | Scan={symbol}"
            )
        else:
            lot_size = get_contract_lot_size(symbol)
            line = f"[SETUP]   {symbol} | Lot size={lot_size}"
        if "options" in engine_name:
            try:
                if not (atm_option_config and symbol == atm_option_config["scan_symbol"]):
                    analytics = get_option_greeks_snapshot(symbol)
                    line += (
                        f" | Premium={analytics['option_price']:.2f}"
                        f" | Underlying={analytics['underlying_price']:.2f}"
                        f" | Delta={analytics['delta']:.3f}"
                        f" | IV={analytics['iv']:.4f}"
                        f" | DTE={analytics.get('days_to_expiry', 'N/A')}"
                    )
            except Exception as exc:
                line += f" | Greeks unavailable ({exc})"
        log_event(line)

    if option_pair_config:
        width = option_pair_config["upper_strike"] - option_pair_config["lower_strike"]
        log_event(
            (
                f"[SETUP]   Structure=TWO_LEG_RANGE | Underlying={option_pair_config['underlying']} "
                f"| Expiry={option_pair_config['expiry']} | Range="
                f"{option_pair_config['lower_strike']}-{option_pair_config['upper_strike']} "
                f"| Width={width}"
            )
        )

    if atm_option_config:
        log_event(
            (
                f"[SETUP]   Structure=ATM_DYNAMIC | Underlying={atm_option_config['underlying']} "
                f"| Expiry={atm_option_config['expiry']} | "
                f"Strike mode={atm_option_config['strike_offset_mode'].replace('_', ' ')} | "
                "Contracts resolved live from underlying movement"
            )
        )


def confirm_selected_fno_contracts(
    engine_name,
    selected_symbols,
    option_pair_config=None,
    atm_option_config=None,
):
    log_selected_fno_contract_summary(
        engine_name,
        selected_symbols,
        option_pair_config,
        atm_option_config,
    )
    confirmation = prompt_choice(
        "Continue with these F&O contracts? YES(1) or NO(2) [default 1]: ",
        [
            {"label": "YES", "key": 1, "value": "YES"},
            {"label": "NO", "key": 2, "value": "NO"},
        ],
        default=1,
    )
    if confirmation != "YES":
        raise SystemExit("[MAIN] F&O contract selection cancelled by user.")


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


def get_stable_signal_data(engine, data, now):
    if data.empty or len(data) < 2:
        return data

    latest_ts = data.index[-1]
    latest_naive = latest_ts.to_pydatetime().replace(tzinfo=None)
    current_minute = now.replace(second=0, microsecond=0)
    if latest_naive >= current_minute and getattr(engine, "require_closed_signal_candle", False):
        return data.iloc[:-1]
    return data


def log_order_signal_banner(title, lines):
    border = "=" * 72
    log_event(border)
    log_event(f"[ORDER] {title}")
    for line in lines:
        log_event(f"[ORDER] {line}")
    log_event(border)


def resolve_atm_option_contract_snapshot(engine, atm_option_config, evaluation, now):
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
    option_data = get_data(
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


def get_cached_regime_context(regime_cache, symbol, trade_day):
    cached = regime_cache.get(symbol)
    if not cached:
        return None
    if cached.get("trade_day") != trade_day.isoformat():
        return None
    return cached.get("context")


def get_pair_symbols(positions, pair_id):
    return [
        symbol
        for symbol, position in positions.items()
        if position.get("pair_id") == pair_id
    ]


def get_pair_position_metrics(positions, pair_symbols, symbol_snapshots):
    entry_total = 0.0
    current_total = 0.0
    total_pnl = 0.0

    for pair_symbol in pair_symbols:
        position = positions.get(pair_symbol)
        snapshot = symbol_snapshots.get(pair_symbol)
        if not position or not snapshot:
            return None
        entry_total += position["entry_price"]
        current_total += snapshot["latest_close"]
        quantity = position["quantity"]
        if position["side"] == "BUY":
            total_pnl += (snapshot["latest_close"] - position["entry_price"]) * quantity
        else:
            total_pnl += (position["entry_price"] - snapshot["latest_close"]) * quantity

    return {
        "entry_total_premium": entry_total,
        "current_total_premium": current_total,
        "total_pnl": total_pnl,
    }


def get_latest_exit_price(engine, symbol, position, symbol_snapshots=None):
    snapshot = (symbol_snapshots or {}).get(symbol)
    if snapshot and snapshot.get("latest_close") is not None:
        return float(snapshot["latest_close"])

    try:
        data = get_data(
            symbol,
            period=engine.data_period,
            interval=engine.data_interval,
        )
        if not data.empty:
            return float(data.iloc[-1]["Close"])
    except Exception as exc:
        log_event(
            f"[REPORT] Could not fetch exit price for {symbol}: {exc}",
            "warning",
        )

    return float(position.get("best_price") or position["entry_price"])


def record_closed_trade(trade_book, symbol, position, exit_price, exit_reason, exit_time):
    quantity = int(position["quantity"])
    entry_price = float(position["entry_price"])
    side = position["side"]
    if side == "BUY":
        pnl = (exit_price - entry_price) * quantity
    else:
        pnl = (entry_price - exit_price) * quantity

    invested_capital = entry_price * quantity
    pnl_pct = (pnl / invested_capital) * 100 if invested_capital > 0 else 0.0

    estimated_charges = 0.0
    net_pnl = pnl
    if (
        TRANSACTION_COST_MODEL_ENABLED
        and position.get("engine_name") == "intraday_equity"
        and symbol.endswith(".NS")
        and ":" not in symbol
    ):
        breakdown = estimate_intraday_equity_round_trip_cost(
            entry_side=side,
            entry_price=entry_price,
            exit_price=float(exit_price),
            quantity=quantity,
            slippage_pct_per_side=float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
        )
        estimated_charges = float(breakdown.total)
        net_pnl = pnl - estimated_charges

    trade_book.append(
        {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entry_time": position.get("entry_time"),
            "exit_time": exit_time.isoformat() if hasattr(exit_time, "isoformat") else str(exit_time),
            "entry_price": entry_price,
            "exit_price": float(exit_price),
            "pnl": pnl,
            "estimated_charges": estimated_charges,
            "net_pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "exit_reason": exit_reason,
            "pair_id": position.get("pair_id"),
        }
    )


def calculate_position_pnl(position, exit_price):
    entry_price = float(position["entry_price"])
    quantity = int(position["quantity"])
    if position["side"] == "BUY":
        pnl = (float(exit_price) - entry_price) * quantity
    else:
        pnl = (entry_price - float(exit_price)) * quantity

    deployed = entry_price * quantity
    pnl_pct = (pnl / deployed) * 100 if deployed > 0 else 0.0
    return pnl, pnl_pct


def build_exit_position_lines(position, exit_price, reason):
    pnl, pnl_pct = calculate_position_pnl(position, exit_price)
    lines = [
        f"Symbol: {position['symbol']}",
        f"EntrySide: {position['side']}",
        f"ExitSide: {'SELL' if position['side'] == 'BUY' else 'BUY'}",
        f"Qty: {position['quantity']}",
        f"EntryPrice: {position['entry_price']:.2f}",
        f"Current/ExitPrice: {float(exit_price):.2f}",
        f"P&L: {pnl:+.2f} ({pnl_pct:+.2f}%)",
        f"Reason: {reason}",
    ]

    if position.get("entry_time"):
        lines.append(f"EntryTime: {format_trade_time(position['entry_time'])}")

    if position.get("stop_loss") is not None:
        lines.append(f"Stop: {position['stop_loss']:.2f}")
    if position.get("target") is not None:
        lines.append(f"Target: {position['target']:.2f}")
    if position.get("trailing_stop") is not None:
        lines.append(f"Trail: {position['trailing_stop']:.2f}")

    return lines


def format_trade_time(raw_value):
    if not raw_value:
        return "-"
    try:
        return datetime.fromisoformat(raw_value).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return str(raw_value)


def log_trade_book_summary(capital, trade_book):
    log_event("[REPORT] CLOSED TRADES")
    if not trade_book:
        log_event("[REPORT]   No closed trades recorded in this session")
        return

    sorted_trades = sorted(trade_book, key=lambda item: item.get("exit_time") or "")
    total_realized = sum(trade["pnl"] for trade in sorted_trades)
    total_estimated_charges = sum(float(trade.get("estimated_charges") or 0.0) for trade in sorted_trades)
    total_net = sum(float(trade.get("net_pnl") or trade["pnl"]) for trade in sorted_trades)
    wins = sum(1 for trade in sorted_trades if trade["pnl"] > 0)
    losses = sum(1 for trade in sorted_trades if trade["pnl"] < 0)
    flats = len(sorted_trades) - wins - losses
    traded_value = sum(trade["entry_price"] * trade["quantity"] for trade in sorted_trades)
    best_trade = max(sorted_trades, key=lambda item: item["pnl"])
    worst_trade = min(sorted_trades, key=lambda item: item["pnl"])

    log_event(
        (
            f"[REPORT]   Closed={len(sorted_trades)} | Wins={wins} | "
            f"Losses={losses} | Flat={flats}"
        )
    )
    log_event(f"[REPORT]   Traded value: {traded_value:.2f}")
    log_event(f"[REPORT]   Realized P&L: {total_realized:+.2f}")
    if TRANSACTION_COST_MODEL_ENABLED:
        log_event(f"[REPORT]   Est. charges: {total_estimated_charges:.2f}")
        log_event(f"[REPORT]   Est. net P&L: {total_net:+.2f}")
    log_event(
        f"[REPORT]   Return on starting capital: {(total_realized / capital) * 100:+.2f}%"
    )
    log_event(
        f"[REPORT]   Best trade: {best_trade['symbol']} {best_trade['pnl']:+.2f}"
    )
    log_event(
        f"[REPORT]   Worst trade: {worst_trade['symbol']} {worst_trade['pnl']:+.2f}"
    )
    log_event(
        "[REPORT]   # | Symbol | Side | Qty | EntryTime | ExitTime | Entry | Exit | P&L | ExitReason"
    )
    for index, trade in enumerate(sorted_trades, start=1):
        pair_suffix = f" [{trade['pair_id']}]" if trade.get("pair_id") else ""
        log_event(
            (
                f"[REPORT]   {index:>2} | {trade['symbol']}{pair_suffix} | "
                f"{trade['side']:<4} | {trade['quantity']:>3} | "
                f"{format_trade_time(trade['entry_time'])} | "
                f"{format_trade_time(trade['exit_time'])} | "
                f"{trade['entry_price']:.2f} | {trade['exit_price']:.2f} | "
                f"{trade['pnl']:+.2f} ({trade['pnl_pct']:+.2f}%) | "
                f"{trade['exit_reason']}"
            )
        )

    # Exit-reason aggregation (matches the “table-style” breakdown from logs)
    summary_rows = summarize_by_exit_reason(sorted_trades)
    log_event("[REPORT] Exit reason summary (gross/net):")
    for row in summary_rows:
        log_event(
            (
                f"[REPORT]   {row['exit_reason']}: "
                f"Trades={row['trades']} | "
                f"Gross={row['gross_pnl']:+.2f} | "
                f"Net={row['net_pnl']:+.2f}"
            )
        )


def close_position_symbols(
    engine,
    positions,
    symbols,
    reason,
    trade_book,
    symbol_snapshots=None,
    exit_time=None,
):
    changed = False
    exit_time = exit_time or datetime.now()
    for symbol in list(symbols):
        position = positions.get(symbol)
        if not position:
            continue
        exit_side = "SELL" if position["side"] == "BUY" else "BUY"
        exit_price = get_latest_exit_price(
            engine,
            symbol,
            position,
            symbol_snapshots=symbol_snapshots,
        )
        log_order_signal_banner(
            "EXIT",
            build_exit_position_lines(position, exit_price, reason),
        )
        place_order(
            exit_side,
            position["quantity"],
            symbol,
            note=reason,
            product=engine.order_product,
        )
        record_closed_trade(
            trade_book,
            symbol,
            position,
            exit_price,
            reason,
            exit_time,
        )
        del positions[symbol]
        changed = True
    return changed


def build_option_pair_candidate(engine, pair_config, symbol_snapshots, positions):
    del engine
    if not pair_config or pair_config.get("mode") != "TWO_LEG_RANGE":
        return None

    pair_id = pair_config["pair_id"]
    if any(position.get("pair_id") == pair_id for position in positions.values()):
        return None

    leg_snapshots = []
    for symbol in pair_config["symbols"]:
        snapshot = symbol_snapshots.get(symbol)
        if not snapshot:
            return None
        leg_snapshots.append(snapshot)

    if any(snapshot["signal"] != "SELL" for snapshot in leg_snapshots):
        return None

    analytics = leg_snapshots[0].get("analytics") or {}
    underlying_price = analytics.get("underlying_price")
    if underlying_price is None:
        return None

    if not (
        pair_config["lower_strike"] <= underlying_price <= pair_config["upper_strike"]
    ):
        log_event(
            (
                f"[PAIR] Underlying {underlying_price:.2f} is outside configured range "
                f"{pair_config['lower_strike']}-{pair_config['upper_strike']}, skipping pair entry"
            )
        )
        return None

    total_premium = sum(snapshot["latest_close"] for snapshot in leg_snapshots)
    average_atr = sum(snapshot["atr"] for snapshot in leg_snapshots) / len(leg_snapshots)
    total_score = sum(snapshot["score"] for snapshot in leg_snapshots)
    return {
        "symbol": pair_id,
        "signal": pair_config.get("entry_side", "SELL"),
        "agreement_count": len(leg_snapshots),
        "score": total_score,
        "latest_close": total_premium,
        "atr": average_atr,
        "analytics": {
            "underlying": analytics.get("underlying"),
            "underlying_price": underlying_price,
        },
        "is_pair": True,
        "pair_config": pair_config,
        "legs": [
            {
                "symbol": symbol,
                "latest_close": symbol_snapshots[symbol]["latest_close"],
                "atr": symbol_snapshots[symbol]["atr"],
                "analytics": symbol_snapshots[symbol].get("analytics"),
                "score": symbol_snapshots[symbol]["score"],
            }
            for symbol in pair_config["symbols"]
        ],
    }


def parse_trade_day(raw_value):
    try:
        return date.fromisoformat(raw_value)
    except (TypeError, ValueError):
        return datetime.now().date()


def save_runtime_state(
    engine_name,
    positions,
    traded_symbols_today,
    trade_counts_today,
    active_trade_day,
    last_entry_time,
    regime_cache,
):
    save_engine_state(
        engine_name=engine_name,
        positions=positions,
        traded_symbols_today=traded_symbols_today,
        trade_counts_today=trade_counts_today,
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
        analytics = candidate.get("analytics") or {}
        greeks_text = ""
        if candidate.get("is_pair"):
            pair_data = candidate.get("pair_config") or {}
            greeks_text = (
                f" | Pair={pair_data.get('lower_strike')}-{pair_data.get('upper_strike')}"
                f" | Underlying={analytics.get('underlying_price', 0.0):.2f}"
            )
        elif analytics:
            greeks_text = (
                f" | Delta={analytics.get('delta', 0.0):.3f}"
                f" | IV={analytics.get('iv', 0.0):.3f}"
            )
        log_event(
            (
                f"[SCAN] Rank {index} | {candidate['symbol']} | "
                f"Signal={candidate['signal']} | "
                f"Agree={candidate['agreement_count']} | "
                f"Score={candidate['score']:.4f} | "
                f"ATR={candidate['atr']:.2f} | "
                f"Last close={candidate['latest_close']:.2f}"
                f"{greeks_text}"
            )
        )


def summarize_execution_stats(engine, capital, positions, trade_book):
    deployed_capital = get_deployed_capital(positions)
    open_count = len(positions)
    open_structure_count = count_open_structures(positions)

    log_event("\n" + "="*50)
    log_event("[STATS] Execution summary:")
    log_event("="*50)
    log_event(f"[STATS] Starting capital: {capital:.2f}")
    log_event(f"[STATS] Open positions: {open_count}")
    log_event(f"[STATS] Open structures: {open_structure_count}")
    log_event(f"[STATS] Deployed capital (entry exposure): {deployed_capital:.2f}")
    log_event(f"[STATS] Capital reserve estimate: {max(0.0, capital - deployed_capital):.2f}")
    log_trade_book_summary(capital, trade_book)

    # Export report after the on-screen/log summary.
    try:
        report_path = export_trade_book_report(trade_book, engine_name=engine.name)
        if report_path:
            log_event(f"[REPORT] Trade report exported: {report_path}")
    except Exception as exc:
        log_event(f"[REPORT] Failed to export trade report: {exc}", "warning")

    if open_count == 0:
        log_event("[STATS] No open positions to evaluate for unrealized P/L")
        log_event("="*50)
        return

    total_market_value = 0.0
    total_unrealized = 0.0
    long_pnl = 0.0
    short_pnl = 0.0
    long_count = 0
    short_count = 0
    long_positions = []
    short_positions = []
    
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
            pnl_pct = ((latest_close - position["entry_price"]) / position["entry_price"]) * 100
            long_pnl += pnl
            long_count += 1
            long_positions.append({
                'symbol': symbol,
                'pnl': pnl,
                'pnl_pct': pnl_pct,
                'quantity': position["quantity"],
                'entry_price': position["entry_price"],
                'current_price': latest_close
            })
        else:
            pnl = (position["entry_price"] - latest_close) * position["quantity"]
            pnl_pct = ((position["entry_price"] - latest_close) / position["entry_price"]) * 100
            short_pnl += pnl
            short_count += 1
            short_positions.append({
                'symbol': symbol,
                'pnl': pnl,
                'pnl_pct': pnl_pct,
                'quantity': position["quantity"],
                'entry_price': position["entry_price"],
                'current_price': latest_close
            })

        total_unrealized += pnl
        log_event(
            (
                f"[STATS] {symbol} {position['side']} qty={position['quantity']} "
                f"entry={position['entry_price']:.2f} current={latest_close:.2f} "
                f"market_value={market_value:.2f} pnl={pnl:+.2f} ({pnl_pct:+.2f}%)"
            )
        )

    log_event("\n" + "-"*50)
    log_event("[STATS] LONG POSITIONS SUMMARY:")
    log_event(f"[STATS]   Count: {long_count}")
    log_event("[STATS]   Stocks:")
    if long_positions:
        for pos in sorted(long_positions, key=lambda x: x['pnl'], reverse=True):
            log_event(f"[STATS]     {pos['symbol']}: {pos['pnl']:+.2f} ({pos['pnl_pct']:+.2f}%)")
    else:
        log_event("[STATS]     None")
    log_event(f"[STATS]   Total P&L: {long_pnl:+.2f}")
    
    log_event("\n[STATS] SHORT POSITIONS SUMMARY:")
    log_event(f"[STATS]   Count: {short_count}")
    log_event("[STATS]   Stocks:")
    if short_positions:
        for pos in sorted(short_positions, key=lambda x: x['pnl'], reverse=True):
            log_event(f"[STATS]     {pos['symbol']}: {pos['pnl']:+.2f} ({pos['pnl_pct']:+.2f}%)")
    else:
        log_event("[STATS]     None")
    log_event(f"[STATS]   Total P&L: {short_pnl:+.2f}")
    
    log_event("\n[STATS] OVERALL RESULTS:")
    log_event(f"[STATS]   Total market value of open positions: {total_market_value:.2f}")
    log_event(f"[STATS]   Total unrealized P&L: {total_unrealized:+.2f}")
    log_event(f"[STATS]   Return %: {(total_unrealized/deployed_capital)*100:+.2f}%" if deployed_capital > 0 else "")
    log_event("="*50)


def force_square_off_positions(engine, positions, trade_book):
    if not positions:
        return False

    log_event("[SQUAREOFF] Closing all open positions")
    changed = False
    exit_time = datetime.now()
    for symbol, position in list(positions.items()):
        exit_side = "SELL" if position["side"] == "BUY" else "BUY"
        exit_price = get_latest_exit_price(engine, symbol, position)
        log_order_signal_banner(
            "FORCE SQUARE OFF",
            build_exit_position_lines(position, exit_price, "Intraday square-off"),
        )
        place_order(
            exit_side,
            position["quantity"],
            symbol,
            note="Intraday square-off",
            product=engine.order_product,
        )
        record_closed_trade(
            trade_book,
            symbol,
            position,
            exit_price,
            "Intraday square-off",
            exit_time,
        )
        del positions[symbol]
        changed = True

    return changed


def manage_open_positions(
    engine,
    positions,
    symbol_snapshots,
    now,
    trade_book,
):
    state_changed = False
    processed_pair_ids = set()
    for symbol, position in list(positions.items()):
        pair_id = position.get("pair_id")
        if pair_id and pair_id in processed_pair_ids:
            continue

        if pair_id:
            pair_symbols = get_pair_symbols(positions, pair_id)
            expected_pair_symbols = position.get("pair_symbols") or []
            if expected_pair_symbols and len(pair_symbols) < len(expected_pair_symbols):
                log_event(
                    f"[PAIR EXIT] {pair_id} leg synchronization guard triggered"
                )
                if close_position_symbols(
                    engine,
                    positions,
                    pair_symbols,
                    reason=f"Pair sync guard for {pair_id}",
                    trade_book=trade_book,
                    symbol_snapshots=symbol_snapshots,
                    exit_time=now,
                ):
                    state_changed = True
                processed_pair_ids.add(pair_id)
                continue

            pair_snapshots = [
                symbol_snapshots.get(pair_symbol)
                for pair_symbol in pair_symbols
            ]
            if any(snapshot is None for snapshot in pair_snapshots):
                log_event(
                    f"[ERROR] Missing latest data for option pair {pair_id}",
                    "error",
                )
                continue

            underlying_price = None
            for snapshot in pair_snapshots:
                analytics = snapshot.get("analytics") or {}
                if analytics.get("underlying_price") is not None:
                    underlying_price = analytics["underlying_price"]
                    break

            lower_strike = position.get("pair_lower_strike")
            upper_strike = position.get("pair_upper_strike")
            if (
                underlying_price is not None
                and lower_strike is not None
                and upper_strike is not None
                and not (lower_strike <= underlying_price <= upper_strike)
            ):
                log_event(
                    (
                        f"[PAIR EXIT] {pair_id} underlying {underlying_price:.2f} "
                        f"breached range {lower_strike}-{upper_strike}"
                    )
                )
                if close_position_symbols(
                    engine,
                    positions,
                    pair_symbols,
                    reason=f"Pair range break {underlying_price:.2f}",
                    trade_book=trade_book,
                    symbol_snapshots=symbol_snapshots,
                    exit_time=now,
                ):
                    state_changed = True
                processed_pair_ids.add(pair_id)
                continue

            if hasattr(engine, "get_time_exit_reason"):
                time_exit_reason = engine.get_time_exit_reason(position, now)
                if time_exit_reason:
                    log_event(f"[PAIR EXIT] {pair_id} {time_exit_reason}")
                    if close_position_symbols(
                        engine,
                        positions,
                        pair_symbols,
                        reason=f"Pair {time_exit_reason}",
                        trade_book=trade_book,
                        symbol_snapshots=symbol_snapshots,
                        exit_time=now,
                    ):
                        state_changed = True
                    processed_pair_ids.add(pair_id)
                    continue

            pair_metrics = get_pair_position_metrics(
                positions,
                pair_symbols,
                symbol_snapshots,
            )
            if pair_metrics:
                log_event(
                    (
                        f"[PAIR] {pair_id} premium {pair_metrics['entry_total_premium']:.2f}"
                        f" -> {pair_metrics['current_total_premium']:.2f}"
                        f" | PnL={pair_metrics['total_pnl']:+.2f}"
                    )
                )
                pair_stop_loss_price = position.get("pair_stop_loss_price")
                pair_target_price = position.get("pair_target_price")
                if (
                    pair_stop_loss_price is not None
                    and pair_metrics["current_total_premium"] >= pair_stop_loss_price
                ):
                    log_event(
                        f"[PAIR EXIT] {pair_id} combined stop hit at "
                        f"{pair_metrics['current_total_premium']:.2f}"
                    )
                    if close_position_symbols(
                        engine,
                        positions,
                        pair_symbols,
                        reason=f"Pair combined stop {pair_metrics['current_total_premium']:.2f}",
                        trade_book=trade_book,
                        symbol_snapshots=symbol_snapshots,
                        exit_time=now,
                    ):
                        state_changed = True
                    processed_pair_ids.add(pair_id)
                    continue
                if (
                    pair_target_price is not None
                    and pair_metrics["current_total_premium"] <= pair_target_price
                ):
                    log_event(
                        f"[PAIR EXIT] {pair_id} combined target hit at "
                        f"{pair_metrics['current_total_premium']:.2f}"
                    )
                    if close_position_symbols(
                        engine,
                        positions,
                        pair_symbols,
                        reason=f"Pair combined target {pair_metrics['current_total_premium']:.2f}",
                        trade_book=trade_book,
                        symbol_snapshots=symbol_snapshots,
                        exit_time=now,
                    ):
                        state_changed = True
                    processed_pair_ids.add(pair_id)
                    continue

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
        if not exit_reason and hasattr(engine, "get_time_exit_reason"):
            exit_reason = engine.get_time_exit_reason(position, now)
        if exit_reason:
            if pair_id:
                pair_symbols = get_pair_symbols(positions, pair_id)
                log_event(
                    f"[PAIR EXIT] {pair_id} {exit_reason} triggered by {symbol} at "
                    f"{snapshot['latest_close']:.2f}"
                )
                if close_position_symbols(
                    engine,
                    positions,
                    pair_symbols,
                    reason=f"Pair exit via {symbol} {exit_reason}",
                    trade_book=trade_book,
                    symbol_snapshots=symbol_snapshots,
                    exit_time=now,
                ):
                    state_changed = True
                processed_pair_ids.add(pair_id)
                continue

            exit_side = "SELL" if position["side"] == "BUY" else "BUY"
            exit_price = float(snapshot["latest_close"])
            log_event(
                f"[EXIT] {symbol} {exit_reason} triggered at "
                f"{exit_price:.2f} | Entry={position['entry_price']:.2f} | "
                f"Qty={position['quantity']}"
            )
            log_order_signal_banner(
                "EXIT",
                build_exit_position_lines(position, exit_price, exit_reason),
            )
            place_order(
                exit_side,
                position["quantity"],
                symbol,
                note=f"Exit {position['side']} via {exit_reason}",
                product=engine.order_product,
            )
            record_closed_trade(
                trade_book,
                symbol,
                position,
                exit_price,
                exit_reason,
                now,
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
            if pair_id:
                pair_symbols = get_pair_symbols(positions, pair_id)
                log_event(
                    f"[PAIR EXIT] Signal-based exit for {pair_id}: "
                    f"{snapshot['signal']} ({signal_exit_reason})"
                )
                if close_position_symbols(
                    engine,
                    positions,
                    pair_symbols,
                    reason=f"Pair close via {signal_exit_reason}",
                    trade_book=trade_book,
                    symbol_snapshots=symbol_snapshots,
                    exit_time=now,
                ):
                    state_changed = True
                processed_pair_ids.add(pair_id)
                continue

            exit_side = "SELL" if position["side"] == "BUY" else "BUY"
            exit_price = float(snapshot["latest_close"])
            log_event(
                f"[EXIT] Signal-based exit for {symbol}: "
                f"{snapshot['signal']} ({signal_exit_reason}) at {exit_price:.2f} | "
                f"Entry={position['entry_price']:.2f} | Qty={position['quantity']}"
            )
            log_order_signal_banner(
                "EXIT",
                build_exit_position_lines(position, exit_price, signal_exit_reason),
            )
            place_order(
                exit_side,
                position["quantity"],
                symbol,
                note=f"Close {position['side']} via {signal_exit_reason}",
                product=engine.order_product,
            )
            record_closed_trade(
                trade_book,
                symbol,
                position,
                exit_price,
                signal_exit_reason,
                now,
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
    log_event("[SETUP]   FUTURES EQUITY: Positional index futures on NIFTY 50 and SENSEX via Kite derivatives")
    log_event("[SETUP]   OPTIONS EQUITY: Positional index options on NIFTY 50 and SENSEX with ATM strike assist")
    log_event("[SETUP]   INTRADAY FUTURES: MIS index futures with auto square-off and lot-aware sizing")
    log_event("[SETUP]   INTRADAY OPTIONS: MIS index options with Greeks/IV filters and auto square-off")

    engine_choice = prompt_choice(
        "Engine: INTRADAY EQUITY(1), DELIVERY EQUITY(2), FUTURES EQUITY(3), OPTIONS EQUITY(4), INTRADAY FUTURES(5), INTRADAY OPTIONS(6)? [default 1]: ",
        [
            {"label": "INTRADAY EQUITY", "key": 1, "value": "1"},
            {"label": "DELIVERY EQUITY", "key": 2, "value": "2"},
            {"label": "FUTURES EQUITY", "key": 3, "value": "3"},
            {"label": "OPTIONS EQUITY", "key": 4, "value": "4"},
            {"label": "INTRADAY FUTURES", "key": 5, "value": "5"},
            {"label": "INTRADAY OPTIONS", "key": 6, "value": "6"},
        ],
        default=1,
    )

    log_event("[SETUP] Enter your trading capital - this is the maximum amount the bot can risk")
    log_event("[SETUP]   For PAPER mode: Use any amount for simulation")
    log_event("[SETUP]   For LIVE mode: Use amount you're comfortable losing")

    capital = prompt_float("Enter capital for strategy: ", minimum=1)

    option_pair_config = None
    atm_option_config = None
    if engine_choice in {"3", "4", "5", "6"}:
        selected_symbols, symbol_mode, option_pair_config, atm_option_config = prompt_fno_contract_selection(
            ENGINE_OPTIONS[engine_choice].name
        )
        confirm_selected_fno_contracts(
            ENGINE_OPTIONS[engine_choice].name,
            selected_symbols,
            option_pair_config=option_pair_config,
            atm_option_config=atm_option_config,
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

    if engine.name == "intraday_options":
        engine.sl_percent = 10.0
        engine.target_percent = 20.0
        engine.trailing_percent = 7.5

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

    if "futures" in engine.name or "options" in engine.name:
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
        if "intraday" in engine.name:
            log_event(
                "[SETUP] Intraday F&O will use MIS product, lot-based quantity rounding, "
                "and auto square-off near market close."
            )
        if engine.name == "intraday_options":
            log_event(
                "[SETUP] Intraday ATM options scalping uses dynamic ATM contract selection, "
                "10% stop-loss, 20% target, and cooldown-managed entries."
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
            "Entry selection: TOP 1(1) or TOP N(2)? [default 1]: ",
            [
                {"label": "TOP 1", "key": 1, "value": "TOP1"},
                {"label": "TOP N", "key": 2, "value": "TOPN"},
            ],
            default=1,
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

    if engine.name == "intraday_options":
        mode = "1"
        strategy_name = prompt_choice(
            (
                "Intraday options strategy: Momentum(1), ORB(2), "
                "VWAP Reversion(3), Multi-strategy(4) [default 1]: "
            ),
            [
                {"label": "MOMENTUM", "key": 1, "value": "ATM_MOMENTUM"},
                {"label": "ORB", "key": 2, "value": "ATM_ORB"},
                {"label": "VWAP REVERSION", "key": 3, "value": "ATM_VWAP_REVERSION"},
                {"label": "MULTI-STRATEGY", "key": 4, "value": "ATM_MULTI"},
            ],
            default=1,
        )
        strategies = None
        min_confirmations = None
        log_event(f"[MAIN] Intraday options strategy selected: {strategy_name}")
    else:
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
    trade_counts_today = {
        str(key): int(value)
        for key, value in saved_state.get("trade_counts_today", {}).items()
    }
    active_trade_day = parse_trade_day(saved_state["active_trade_day"])
    last_entry_time = float(saved_state["last_entry_time"])
    regime_cache = saved_state["regime_cache"]
    trade_book = []
    save_runtime_state(
        engine.name,
        positions,
        traded_symbols_today,
        trade_counts_today,
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
            trade_counts_today.clear()
            log_event("[MAIN] New day detected, reset traded symbol tracker")
            if engine.order_product == "MIS" and positions:
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
                trade_counts_today,
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
            if force_square_off_positions(engine, positions, trade_book):
                save_runtime_state(
                    engine.name,
                    positions,
                    traded_symbols_today,
                    trade_counts_today,
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
            signal_data = get_stable_signal_data(engine, data, now)
            if signal_data.empty:
                log_event(
                    f"[SCAN] {symbol} has no fully closed candle available yet, skipping signal evaluation",
                    "warning",
                )
                continue
            active_mode = mode
            active_strategy_name = strategy_name
            active_strategies = strategies
            active_min_confirmations = min_confirmations
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
                and atm_option_config is not None
                and symbol == atm_option_config["scan_symbol"]
            )
            contract_data = signal_data

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
                            trade_counts_today,
                            active_trade_day,
                            last_entry_time,
                            regime_cache,
                        )
                log_market_context(symbol, market_context)
                active_mode = "2"
                active_strategy_name = None
                active_strategies = market_context["strategies"]
                active_min_confirmations = market_context["min_confirmations"]

            if engine.name == "intraday_options" and atm_option_config and not dynamic_atm_scan:
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
                        atm_option_config,
                        evaluation,
                        now,
                    )
                    candidate_symbol = contract_snapshot["symbol"]
                    candidate_latest_close = contract_snapshot["latest_close"]
                    candidate_latest_candle = contract_snapshot["latest_candle"]
                    candidate_atr = contract_snapshot["atr"]
                    trade_identity = contract_snapshot["trade_identity"]
                    option_analytics = contract_snapshot["analytics"]
                    contract_data = contract_snapshot["data"]
                    log_event(
                        (
                            f"[ATM] {symbol} -> {evaluation['option_signal']} -> "
                            f"{candidate_symbol} | Premium={candidate_latest_close:.2f}"
                        )
                    )
                except Exception as exc:
                    log_event(
                        f"[ATM] Could not resolve ATM contract for {symbol}: {exc}",
                        "warning",
                    )
                    evaluation["signal"] = "HOLD"
                    evaluation["agreement_count"] = 0
                    evaluation["score"] = 0.0
                    evaluation["reason"] = str(exc)

            elif "options" in engine.name:
                try:
                    option_analytics = get_option_greeks_snapshot(symbol)
                    if (
                        engine.name == "intraday_options"
                        and option_pair_config
                        and symbol in option_pair_config.get("symbols", [])
                    ):
                        option_analytics["skip_underlying_bias"] = True
                except Exception as exc:
                    log_event(
                        f"[GREEKS] Could not build options analytics for {symbol}: {exc}",
                        "warning",
                    )
            if hasattr(engine, "apply_signal_filters"):
                evaluation = engine.apply_signal_filters(
                    evaluation,
                    contract_data,
                    intraday_history_df=intraday_history,
                    min_confirmations=active_min_confirmations or 1,
                    analytics=option_analytics,
                )

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

            log_event(
                (
                    f"[SCAN] {symbol} | Signal={evaluation['signal']} | "
                    f"Agree={evaluation['agreement_count']} | "
                    f"Score={evaluation['score']:.4f} | "
                    f"ATR={symbol_snapshots[symbol]['atr']:.2f} | "
                    f"Last close={candidate_latest_close:.2f} | "
                    f"VWAP bias={evaluation.get('vwap_bias', 'N/A')} | "
                    f"Range%={evaluation.get('range_pct', 0.0):.2f} | "
                    f"Underlying bias={evaluation.get('underlying_bias', 'N/A')}"
                )
            )
            if evaluation.get("reason"):
                log_event(f"[SCAN] {symbol} | Reason: {evaluation['reason']}")
            if evaluation.get("breakout_volume_note"):
                log_event(
                    f"[SCAN] {symbol} | Breakout volume filter: "
                    f"{evaluation['breakout_volume_note']}"
                )
            if option_analytics:
                log_event(
                    (
                        f"[GREEKS] {symbol} | Underlying={option_analytics['underlying_price']:.2f} | "
                        f"Premium={option_analytics['option_price']:.2f} | "
                        f"IV={option_analytics['iv']:.4f} | "
                        f"IV15m={option_analytics.get('iv_change_15m_pct', 'N/A')} | "
                        f"Delta={option_analytics['delta']:.4f} | "
                        f"Gamma={option_analytics['gamma']:.6f} | "
                        f"Theta={option_analytics['theta']:.4f} | "
                        f"Vega={option_analytics['vega']:.4f} | "
                        f"DTE={option_analytics.get('days_to_expiry', 'N/A')} | "
                        f"IVPct={option_analytics['iv_percentile'] if option_analytics['iv_percentile'] is not None else 'N/A'}"
                    )
                )
            if evaluation.get("options_filter_note"):
                log_event(
                    f"[SCAN] {symbol} | Options filter: "
                    f"{evaluation['options_filter_note']}"
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
            if (
                normalized_signal
                and engine.name == "intraday_options"
                and option_pair_config
                and symbol in option_pair_config.get("symbols", [])
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
                    }
                )

        if not symbol_snapshots and positions:
            log_event("[ERROR] No symbol data available for open positions", "error")
        elif not symbol_snapshots:
            log_event("[ERROR] No symbol data available in this cycle", "error")
            time.sleep(engine.sleep_seconds)
            continue

        if engine.name == "intraday_options" and option_pair_config:
            pair_candidate = build_option_pair_candidate(
                engine,
                option_pair_config,
                symbol_snapshots,
                positions,
            )
            if pair_candidate:
                candidates.append(pair_candidate)

        ranked_candidates = rank_candidates(candidates)
        log_ranked_candidates(ranked_candidates)

        state_changed = False
        if cycle_state["manage_positions"]:
            state_changed = manage_open_positions(
                engine,
                positions,
                symbol_snapshots,
                now,
                trade_book,
            )
            if state_changed:
                save_runtime_state(
                    engine.name,
                    positions,
                    traded_symbols_today,
                    trade_counts_today,
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

                if candidate.get("is_pair"):
                    pair_config = candidate["pair_config"]
                    pair_symbols = pair_config["symbols"]
                    pair_id = pair_config["pair_id"]

                    if any(pair_symbol in positions for pair_symbol in pair_symbols):
                        log_event(f"[LIMIT] Pair {pair_id} already has an open leg")
                        continue

                    if one_trade_per_symbol_per_day and any(
                        pair_symbol in traded_symbols_today
                        for pair_symbol in pair_symbols
                    ):
                        log_event(
                            f"[LIMIT] Pair {pair_id} already traded today on one of its legs"
                        )
                        continue

                    if count_open_structures(positions) >= max_open_positions:
                        log_event(
                            f"[LIMIT] Max open position structures would be exceeded by pair {pair_id}"
                        )
                        continue

                    trade_key = None
                    max_trades_per_day = 0
                    if hasattr(engine, "get_trade_frequency_key") and hasattr(engine, "get_max_trades_per_day"):
                        trade_key = engine.get_trade_frequency_key(
                            pair_id,
                            candidate.get("analytics"),
                        )
                        max_trades_per_day = engine.get_max_trades_per_day()
                        if trade_key and max_trades_per_day > 0:
                            trade_count = int(trade_counts_today.get(trade_key, 0))
                            if trade_count >= max_trades_per_day:
                                log_event(
                                    f"[LIMIT] {trade_key} reached max intraday option trades "
                                    f"for the day ({trade_count}/{max_trades_per_day})"
                                )
                                continue

                    leg_entries = []
                    pair_premium = sum(leg["latest_close"] for leg in candidate["legs"])
                    if pair_premium <= 0:
                        log_event(f"[RISK] Invalid pair premium for {pair_id}", "warning")
                        continue

                    per_trade_cap_lots = int(max_capital_per_trade / pair_premium)
                    remaining_deployable = max(0.0, max_capital_deployed - deployed_capital)
                    deploy_cap_lots = int(remaining_deployable / pair_premium)
                    max_pair_lots = min(per_trade_cap_lots, deploy_cap_lots)

                    for leg in candidate["legs"]:
                        leg_symbol = leg["symbol"]
                        leg_entry_price = leg["latest_close"]
                        leg_atr = leg.get("atr", 0.0)
                        leg_stop_data = atr_stop_from_value(
                            candidate["signal"],
                            leg_entry_price,
                            leg_atr,
                            atr_stop_multiplier,
                        )
                        if leg_stop_data["stop_distance"] <= 0:
                            max_pair_lots = 0
                            break

                        leg_lot_size = get_contract_lot_size(leg_symbol)
                        leg_sizing = atr_position_size(
                            capital=capital,
                            entry_price=leg_entry_price,
                            atr_value=leg_atr,
                            atr_multiplier=atr_stop_multiplier,
                            risk_percent=risk_percent,
                        )
                        risk_lots = leg_sizing["quantity"] // leg_lot_size
                        leg_qty_cap = engine.apply_entry_allocation_limit(
                            leg_symbol,
                            max(leg_lot_size, max_pair_lots * leg_lot_size),
                            leg_entry_price,
                            positions,
                            capital,
                        )
                        allocation_lots = leg_qty_cap // leg_lot_size
                        max_pair_lots = min(max_pair_lots, risk_lots, allocation_lots)
                        leg_entries.append(
                            {
                                "symbol": leg_symbol,
                                "entry_price": leg_entry_price,
                                "atr": leg_atr,
                                "stop_data": leg_stop_data,
                                "lot_size": leg_lot_size,
                                "analytics": leg.get("analytics"),
                            }
                        )

                    if max_pair_lots <= 0:
                        log_event(
                            f"[RISK] Pair quantity is 0 for {pair_id} after limits",
                            "warning",
                        )
                        continue

                    estimated_trade_capital = 0.0
                    entered_pair_symbols = []
                    pair_target_price = calculate_target_price(
                        candidate["signal"],
                        pair_premium,
                        pair_premium * (target_percent / 100.0),
                    )
                    pair_stop_loss_price = (
                        pair_premium * (1 - (sl_percent / 100.0))
                        if candidate["signal"] == "BUY"
                        else pair_premium * (1 + (sl_percent / 100.0))
                    )
                    log_event(
                        (
                            f"[PAIR ENTRY] Executing bounded range pair {pair_id} | "
                            f"Underlying={candidate['analytics'].get('underlying_price', 0.0):.2f} | "
                            f"Range={pair_config['lower_strike']}-{pair_config['upper_strike']} | "
                            f"Lots={max_pair_lots} | Combined SL={pair_stop_loss_price:.2f} "
                            f"| Combined Target={pair_target_price:.2f}"
                        )
                    )

                    for leg_entry in leg_entries:
                        leg_symbol = leg_entry["symbol"]
                        qty = max_pair_lots * leg_entry["lot_size"]
                        target_distance = leg_entry["stop_data"]["stop_distance"] * target_risk_reward
                        trailing_distance = leg_entry["atr"] * trailing_atr_multiplier
                        target_price = calculate_target_price(
                            candidate["signal"],
                            leg_entry["entry_price"],
                            target_distance,
                        )
                        # Same trailing activation behavior for pair legs.
                        trailing_stop = float(leg_entry["stop_data"]["stop_loss_price"])
                        trailing_activation_distance = max(
                            float(trailing_distance or 0.0),
                            float(leg_entry["stop_data"].get("stop_distance") or 0.0)
                            * float(TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER or 0.0),
                        )
                        try:
                            log_order_signal_banner(
                                "PAIR LEG ENTRY",
                                [
                                    f"Structure: {pair_id}",
                                    f"Leg: {leg_symbol}",
                                    f"Side: {candidate['signal']}",
                                    f"Qty: {qty}",
                                    f"Entry: {leg_entry['entry_price']:.2f}",
                                    f"Stop: {leg_entry['stop_data']['stop_loss_price']:.2f}",
                                    f"Target: {target_price:.2f}",
                                    f"Trail: {trailing_stop:.2f}",
                                ],
                            )
                            order_id = place_order(
                                candidate["signal"],
                                qty,
                                leg_symbol,
                                note=f"Pair entry {pair_id}",
                                product=engine.order_product,
                            )
                            log_event(
                                f"[ORDER] Pair leg accepted | Symbol={leg_symbol} | OrderId={order_id}"
                            )
                        except Exception:
                            if entered_pair_symbols:
                                log_event(
                                    f"[PAIR EXIT] Pair entry failed on {leg_symbol}; "
                                    "closing already-entered legs to avoid partial exposure",
                                    "warning",
                                )
                                close_position_symbols(
                                    engine,
                                    positions,
                                    entered_pair_symbols,
                                    reason=f"Pair sync unwind {pair_id}",
                                    trade_book=trade_book,
                                    exit_time=now,
                                )
                            raise
                        positions[leg_symbol] = build_position(
                            symbol=leg_symbol,
                            side=candidate["signal"],
                            quantity=qty,
                            entry_price=leg_entry["entry_price"],
                            stop_loss=leg_entry["stop_data"]["stop_loss_price"],
                            target=target_price,
                            trailing_stop=trailing_stop,
                            trailing_distance=trailing_distance,
                            trailing_activation_distance=trailing_activation_distance,
                            trailing_active=False,
                            atr=leg_entry["atr"],
                            stop_distance=leg_entry["stop_data"]["stop_distance"],
                            lot_size=leg_entry["lot_size"],
                            entry_analytics=leg_entry["analytics"],
                            pair_id=pair_id,
                            pair_mode=pair_config["mode"],
                            pair_underlying=pair_config["underlying"],
                            pair_lower_strike=pair_config["lower_strike"],
                            pair_upper_strike=pair_config["upper_strike"],
                            pair_symbols=pair_symbols,
                            pair_entry_total_premium=pair_premium,
                            pair_stop_loss_price=pair_stop_loss_price,
                            pair_target_price=pair_target_price,
                            entry_time=now.isoformat(),
                            engine_name=engine.name,
                            order_product=engine.order_product,
                        )
                        entered_pair_symbols.append(leg_symbol)
                        traded_symbols_today.add(leg_symbol)
                        estimated_trade_capital += leg_entry["entry_price"] * qty

                    if trade_key:
                        trade_counts_today[trade_key] = int(
                            trade_counts_today.get(trade_key, 0)
                        ) + 1
                    deployed_capital += estimated_trade_capital
                    last_entry_time = current_time
                    log_event(f"[RISK] Updated deployed capital: {deployed_capital:.2f}")
                    save_runtime_state(
                        engine.name,
                        positions,
                        traded_symbols_today,
                        trade_counts_today,
                        active_trade_day,
                        last_entry_time,
                        regime_cache,
                    )

                    if entry_selection_mode == "TOP1":
                        break
                    continue

                if symbol in positions:
                    log_event(f"[LIMIT] {symbol} already has an open position")
                    continue

                trade_identity = candidate.get("trade_identity", symbol)
                if one_trade_per_symbol_per_day and trade_identity in traded_symbols_today:
                    log_event(
                        f"[LIMIT] {trade_identity} already traded today, skipping"
                    )
                    continue

                if engine.name == "intraday_options" and atm_option_config:
                    same_underlying_open = any(
                        (
                            (position.get("entry_analytics") or {}).get("underlying")
                            == trade_identity
                        )
                        for position in positions.values()
                    )
                    if same_underlying_open:
                        log_event(
                            f"[LIMIT] {trade_identity} already has an open ATM options position"
                        )
                        continue

                if hasattr(engine, "get_trade_frequency_key") and hasattr(engine, "get_max_trades_per_day"):
                    trade_key = engine.get_trade_frequency_key(
                        symbol,
                        candidate.get("analytics"),
                    )
                    max_trades_per_day = engine.get_max_trades_per_day()
                    if trade_key and max_trades_per_day > 0:
                        trade_count = int(trade_counts_today.get(trade_key, 0))
                        if trade_count >= max_trades_per_day:
                            log_event(
                                f"[LIMIT] {trade_key} reached max intraday option trades "
                                f"for the day ({trade_count}/{max_trades_per_day})"
                            )
                            continue

                if count_open_structures(positions) >= max_open_positions:
                    log_event("[LIMIT] Max open position structures reached")
                    break

                entry_price = candidate["latest_close"]
                atr_value = candidate.get("atr", 0.0)
                if engine.name == "intraday_options" and atm_option_config:
                    stop_distance = entry_price * 0.10
                    stop_loss_price = calculate_stop_loss_price(
                        candidate["signal"],
                        entry_price,
                        stop_distance,
                    )
                    stop_data = {
                        "atr": atr_value,
                        "stop_distance": stop_distance,
                        "stop_loss_price": stop_loss_price,
                    }
                    qty = position_size(
                        capital=capital,
                        entry_price=entry_price,
                        stop_loss_price=stop_loss_price,
                        risk_percent=risk_percent,
                    )
                else:
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
                if engine.name == "intraday_options" and atm_option_config:
                    target_distance = entry_price * 0.20
                    trailing_distance = entry_price * 0.075
                else:
                    target_distance = stop_data["stop_distance"] * target_risk_reward
                    trailing_distance = atr_value * trailing_atr_multiplier
                target_price = calculate_target_price(
                    candidate["signal"],
                    entry_price,
                    target_distance,
                )
                # Trailing should not behave like an extra-tight stop from the first minute.
                # Start trailing at the stop-loss level and only activate once price moves
                # enough in favor (see trailing_activation_distance below).
                trailing_stop = float(stop_data["stop_loss_price"])

                # Trailing activation: delay trailing until price has moved in favor enough.
                # This reduces 1-minute whipsaw exits where trailing behaves like a tight stop.
                trailing_activation_distance = max(
                    float(trailing_distance or 0.0),
                    float(stop_data.get("stop_distance") or 0.0)
                    * float(TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER or 0.0),
                )

                # Transaction-cost filter: reject trades whose expected edge proxy is smaller
                # than estimated costs + buffer.
                if (
                    TRANSACTION_COST_MODEL_ENABLED
                    and engine.name == "intraday_equity"
                    and symbol.endswith(".NS")
                    and ":" not in symbol
                ):
                    breakdown = estimate_intraday_equity_round_trip_cost(
                        entry_side=str(candidate.get("signal") or "BUY"),
                        entry_price=float(entry_price),
                        exit_price=float(entry_price),  # pre-trade: assume flat for cost estimation
                        quantity=int(qty),
                        slippage_pct_per_side=float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
                    )
                    est_cost = float(breakdown.total)
                    expected_edge_points = float(entry_price) * float(candidate.get("score") or 0.0) * float(
                        EXPECTED_EDGE_SCORE_MULTIPLIER or 1.0
                    )
                    expected_edge_rupees = expected_edge_points * int(qty)
                    required_edge = (est_cost * float(MIN_EDGE_TO_COST_RATIO or 1.0)) + float(
                        COST_EDGE_BUFFER_RUPEES or 0.0
                    )
                    if expected_edge_rupees < required_edge:
                        log_event(
                            (
                                f"[FILTER] Skipping {symbol} due to low edge vs cost | "
                                f"Score={candidate.get('score', 0.0):.4f} | "
                                f"ExpectedEdge≈{expected_edge_rupees:.2f} | "
                                f"EstCost≈{est_cost:.2f} | "
                                f"Required≥{required_edge:.2f}"
                            )
                        )
                        continue

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
                entry_lines = [
                    f"Symbol: {symbol}",
                    f"Side: {candidate['signal']}",
                    f"Qty: {qty}",
                    f"Entry: {entry_price:.2f}",
                    f"Stop: {stop_data['stop_loss_price']:.2f}",
                    f"Target: {target_price:.2f}",
                    f"Trail: {trailing_stop:.2f}",
                    f"Score: {candidate['score']:.4f}",
                ]
                if candidate.get("analytics"):
                    analytics = candidate["analytics"]
                    entry_lines.append(
                        f"Underlying: {analytics.get('underlying', 'N/A')} @ {analytics.get('underlying_price', 0.0):.2f}"
                    )
                    entry_lines.append(
                        f"OptionType: {(analytics.get('option_type') or 'N/A').upper()} | StrikeMode: {atm_option_config.get('strike_offset_mode', 'N/A') if atm_option_config else 'N/A'}"
                    )
                log_order_signal_banner("SINGLE ENTRY", entry_lines)
                order_id = place_order(
                    candidate["signal"],
                    qty,
                    symbol,
                    note="Entry",
                    product=engine.order_product,
                )
                log_event(f"[ORDER] Entry accepted | Symbol={symbol} | OrderId={order_id}")
                positions[symbol] = build_position(
                    symbol=symbol,
                    side=candidate["signal"],
                    quantity=qty,
                    entry_price=entry_price,
                    stop_loss=stop_data["stop_loss_price"],
                    target=target_price,
                    trailing_stop=trailing_stop,
                    trailing_distance=trailing_distance,
                    trailing_activation_distance=trailing_activation_distance,
                    trailing_active=False,
                    atr=atr_value,
                    stop_distance=stop_data["stop_distance"],
                    lot_size=get_contract_lot_size(symbol) if ":" in symbol else 1,
                    entry_analytics=candidate.get("analytics"),
                    entry_time=now.isoformat(),
                    engine_name=engine.name,
                    order_product=engine.order_product,
                )
                traded_symbols_today.add(trade_identity)
                if hasattr(engine, "get_trade_frequency_key"):
                    trade_key = engine.get_trade_frequency_key(
                        symbol,
                        candidate.get("analytics"),
                    )
                    if trade_key:
                        trade_counts_today[trade_key] = int(
                            trade_counts_today.get(trade_key, 0)
                        ) + 1
                deployed_capital += estimated_trade_capital
                last_entry_time = current_time
                log_event(f"[RISK] Updated deployed capital: {deployed_capital:.2f}")
                save_runtime_state(
                    engine.name,
                    positions,
                    traded_symbols_today,
                    trade_counts_today,
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
    if "positions" in locals() and len(positions) > 0:
        log_event(f"\n[MAIN] You have {len(positions)} open position(s).")
        close_choice = input("\nClose all positions? (YES/NO) [default NO]: ").strip().upper()
        if close_choice == "YES":
            confirm = input("Are you sure? This will close ALL positions immediately. (YES/NO): ").strip().upper()
            if confirm == "YES":
                log_event("[MAIN] Closing all open positions...")
                exit_time = datetime.now()
                for symbol, position in list(positions.items()):
                    exit_side = "SELL" if position["side"] == "BUY" else "BUY"
                    exit_price = get_latest_exit_price(
                        engine,
                        symbol,
                        position,
                    ) if "engine" in locals() else float(position["entry_price"])
                    log_event(f"[MAIN] Closing {symbol}: {position['side']} {position['quantity']} units at market")
                    place_order(
                        exit_side,
                        position["quantity"],
                        symbol,
                        note="User-initiated emergency close-out",
                        product=engine.order_product if "engine" in locals() else "MIS",
                    )
                    if "trade_book" in locals():
                        record_closed_trade(
                            trade_book,
                            symbol,
                            position,
                            exit_price,
                            "User-initiated emergency close-out",
                            exit_time,
                        )
                    del positions[symbol]
                log_event("[MAIN] All positions closed.")
            else:
                log_event("[MAIN] Close cancelled. Keeping positions open.")
        else:
            log_event("[MAIN] Positions remain open. Please manage them manually.")
except Exception as exc:
    log_event(f"\n[ERROR] {exc}", "error")
    logger.exception("[MAIN] Unhandled exception")
    raise
finally:
    if "positions" in locals() and "engine" in locals() and "capital" in locals():
        try:
            summarize_execution_stats(
                engine,
                capital,
                positions,
                trade_book if "trade_book" in locals() else [],
            )
        except Exception as exc:
            log_event(f"[STATS] Failed to generate summary: {exc}", "warning")

    session_log_path = finalize_session_logger()
    if session_log_path:
        print(f"[LOG] Session log saved to {session_log_path}")
