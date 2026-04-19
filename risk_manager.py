from logger import get_logger


logger = get_logger()


def calculate_stop_loss_price(side, entry_price, stop_distance):
    if side == "BUY":
        return entry_price - stop_distance
    return entry_price + stop_distance


def calculate_target_price(side, entry_price, target_distance):
    if side == "BUY":
        return entry_price + target_distance
    return entry_price - target_distance


def atr_stop_from_value(side, entry_price, atr_value, atr_multiplier):
    stop_distance = max(0.0, float(atr_value) * float(atr_multiplier))
    stop_loss_price = calculate_stop_loss_price(
        side,
        float(entry_price),
        stop_distance,
    )
    return {
        "atr": float(atr_value),
        "stop_distance": stop_distance,
        "stop_loss_price": stop_loss_price,
    }


def position_size(
    capital,
    entry_price,
    stop_loss_price,
    risk_percent,
):
    risk_amount = capital * risk_percent
    per_share_risk = abs(entry_price - stop_loss_price)

    if per_share_risk <= 0:
        qty = 0
    else:
        risk_based_qty = int(risk_amount / per_share_risk)
        affordable_qty = int(capital / entry_price) if entry_price > 0 else 0
        qty = min(risk_based_qty, affordable_qty)

    print(f"\n[RISK] Capital: {capital}")
    print(f"[RISK] Risk %: {risk_percent * 100:.2f}")
    print(f"[RISK] Entry price: {entry_price:.2f}")
    print(f"[RISK] Stop-loss price: {stop_loss_price:.2f}")
    print(f"[RISK] Per share risk: {per_share_risk:.2f}")
    print(f"[RISK] Quantity decided: {qty}")
    logger.info(f"[RISK] Capital: {capital}")
    logger.info(f"[RISK] Risk %: {risk_percent * 100:.2f}")
    logger.info(f"[RISK] Entry price: {entry_price:.2f}")
    logger.info(f"[RISK] Stop-loss price: {stop_loss_price:.2f}")
    logger.info(f"[RISK] Per share risk: {per_share_risk:.2f}")
    logger.info(f"[RISK] Quantity decided: {qty}")

    return qty


def atr_position_size(
    capital,
    entry_price,
    atr_value,
    atr_multiplier,
    risk_percent,
):
    stop_details = atr_stop_from_value(
        side="BUY",
        entry_price=entry_price,
        atr_value=atr_value,
        atr_multiplier=atr_multiplier,
    )
    stop_distance = stop_details["stop_distance"]
    risk_amount = capital * risk_percent

    if stop_distance <= 0:
        qty = 0
    else:
        risk_based_qty = int(risk_amount / stop_distance)
        affordable_qty = int(capital / entry_price) if entry_price > 0 else 0
        qty = min(risk_based_qty, affordable_qty)

    logger.info(
        "[RISK] ATR sizing | Capital=%s | Entry=%s | ATR=%s | Multiplier=%s | "
        "Stop distance=%s | Risk %%=%s | Qty=%s",
        capital,
        entry_price,
        atr_value,
        atr_multiplier,
        stop_distance,
        risk_percent,
        qty,
    )
    return {
        "quantity": qty,
        "risk_amount": risk_amount,
        "atr": float(atr_value),
        "stop_distance": stop_distance,
    }
