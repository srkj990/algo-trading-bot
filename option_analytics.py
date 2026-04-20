import math
from datetime import date, datetime


SQRT_2PI = math.sqrt(2.0 * math.pi)


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value).date()
    raise ValueError(f"Unsupported date value: {value}")


def _cdf(value):
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _pdf(value):
    return math.exp(-0.5 * value * value) / SQRT_2PI


def years_to_expiry(expiry, as_of=None):
    expiry_date = _as_date(expiry)
    current_date = _as_date(as_of or datetime.now())
    days = max((expiry_date - current_date).days, 0)
    return max(days / 365.0, 1 / 365.0)


def _d1_d2(spot, strike, time_to_expiry, risk_free_rate, volatility):
    if (
        spot <= 0
        or strike <= 0
        or time_to_expiry <= 0
        or volatility <= 0
    ):
        return None, None

    sigma_sqrt_t = volatility * math.sqrt(time_to_expiry)
    d1 = (
        math.log(spot / strike)
        + (risk_free_rate + 0.5 * volatility * volatility) * time_to_expiry
    ) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    return d1, d2


def black_scholes_price(
    spot,
    strike,
    time_to_expiry,
    risk_free_rate,
    volatility,
    option_type,
):
    option_side = (option_type or "").upper()
    d1, d2 = _d1_d2(
        float(spot),
        float(strike),
        float(time_to_expiry),
        float(risk_free_rate),
        float(volatility),
    )
    if d1 is None:
        return 0.0

    discounted_strike = strike * math.exp(-risk_free_rate * time_to_expiry)
    if option_side == "CE":
        return (spot * _cdf(d1)) - (discounted_strike * _cdf(d2))
    if option_side == "PE":
        return (discounted_strike * _cdf(-d2)) - (spot * _cdf(-d1))
    raise ValueError(f"Unsupported option type: {option_type}")


def implied_volatility(
    option_price,
    spot,
    strike,
    time_to_expiry,
    risk_free_rate,
    option_type,
    tolerance=1e-5,
    max_iterations=100,
):
    premium = float(option_price)
    if premium <= 0 or spot <= 0 or strike <= 0 or time_to_expiry <= 0:
        return 0.0

    low = 1e-4
    high = 5.0
    for _ in range(max_iterations):
        mid = (low + high) / 2.0
        estimate = black_scholes_price(
            spot,
            strike,
            time_to_expiry,
            risk_free_rate,
            mid,
            option_type,
        )
        error = estimate - premium
        if abs(error) <= tolerance:
            return mid
        if error > 0:
            high = mid
        else:
            low = mid

    return (low + high) / 2.0


def calculate_greeks(
    spot,
    strike,
    time_to_expiry,
    risk_free_rate,
    volatility,
    option_type,
):
    option_side = (option_type or "").upper()
    d1, d2 = _d1_d2(
        float(spot),
        float(strike),
        float(time_to_expiry),
        float(risk_free_rate),
        float(volatility),
    )
    if d1 is None:
        return {
            "delta": 0.0,
            "gamma": 0.0,
            "theta": 0.0,
            "vega": 0.0,
            "rho": 0.0,
        }

    sqrt_t = math.sqrt(time_to_expiry)
    pdf_d1 = _pdf(d1)
    gamma = pdf_d1 / (spot * volatility * sqrt_t)
    vega = (spot * pdf_d1 * sqrt_t) / 100.0

    if option_side == "CE":
        delta = _cdf(d1)
        theta = (
            (-spot * pdf_d1 * volatility) / (2.0 * sqrt_t)
            - risk_free_rate
            * strike
            * math.exp(-risk_free_rate * time_to_expiry)
            * _cdf(d2)
        ) / 365.0
        rho = (
            strike
            * time_to_expiry
            * math.exp(-risk_free_rate * time_to_expiry)
            * _cdf(d2)
        ) / 100.0
    elif option_side == "PE":
        delta = _cdf(d1) - 1.0
        theta = (
            (-spot * pdf_d1 * volatility) / (2.0 * sqrt_t)
            + risk_free_rate
            * strike
            * math.exp(-risk_free_rate * time_to_expiry)
            * _cdf(-d2)
        ) / 365.0
        rho = (
            -strike
            * time_to_expiry
            * math.exp(-risk_free_rate * time_to_expiry)
            * _cdf(-d2)
        ) / 100.0
    else:
        raise ValueError(f"Unsupported option type: {option_type}")

    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
    }
