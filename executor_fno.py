import executor

from logger import log_event


def place_fno_order(signal, quantity, symbol, product="NRML", note=None):
    log_event("[EXECUTION_FNO] Routing F&O order through executor.place_order")
    return executor.place_order(
        signal,
        quantity,
        symbol,
        note=note,
        product=product,
    )


def get_futures_positions(product=None):
    positions = executor.get_nfo_positions()
    futures = []
    for item in positions:
        tradingsymbol = item.get("tradingsymbol")
        if not tradingsymbol or not tradingsymbol.upper().endswith("FUT"):
            continue
        if product and (item.get("product") or "").upper() != product.upper():
            continue
        futures.append(item)
    return futures


def get_options_positions(product=None):
    positions = executor.get_nfo_positions()
    options = []
    for item in positions:
        tradingsymbol = item.get("tradingsymbol")
        if not tradingsymbol:
            continue
        upper_symbol = tradingsymbol.upper()
        if upper_symbol.endswith("CE") or upper_symbol.endswith("PE"):
            if product and (item.get("product") or "").upper() != product.upper():
                continue
            options.append(item)
    return options
