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

    logger.info("[RISK] Capital: %s", capital)
    logger.info("[RISK] Risk %%: %.2f", risk_percent * 100)
    logger.info("[RISK] Entry price: %.2f", entry_price)
    logger.info("[RISK] Stop-loss price: %.2f", stop_loss_price)
    logger.info("[RISK] Per share risk: %.2f", per_share_risk)
    logger.info("[RISK] Quantity decided: %s", qty)

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


def check_daily_loss_limit(session_pnl, capital, max_loss_pct):
    max_loss_pct = float(max_loss_pct or 0.0)
    if max_loss_pct <= 0 or float(capital or 0.0) <= 0:
        return False
    return float(session_pnl or 0.0) <= -(float(capital) * max_loss_pct)
