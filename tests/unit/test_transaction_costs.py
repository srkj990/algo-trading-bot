import pytest

from transaction_costs import calculate_option_charges, estimate_options_round_trip_cost


def test_calculate_option_charges_example_trade():
    breakdown = calculate_option_charges(buy_price=151.30, sell_price=156.35, quantity=65)

    assert breakdown["total_charges"] == pytest.approx(71.14, abs=2.0)

    buy_turnover = 151.30 * 65
    sell_turnover = 156.35 * 65
    total_turnover = buy_turnover + sell_turnover

    assert breakdown["brokerage"] == pytest.approx(40.0)
    assert breakdown["stt"] == pytest.approx(sell_turnover * 0.0015, abs=0.01)
    assert breakdown["transaction_charges"] == pytest.approx(total_turnover * 0.0003553, abs=0.01)
    assert breakdown["sebi"] == pytest.approx(total_turnover * (10 / 10_000_000), abs=0.01)
    assert breakdown["stamp_duty"] == pytest.approx(buy_turnover * 0.00003, abs=0.01)
    assert breakdown["gst"] == pytest.approx((breakdown["brokerage"] + breakdown["transaction_charges"]) * 0.18, abs=0.01)

    expected_total = (
        breakdown["brokerage"]
        + breakdown["stt"]
        + breakdown["transaction_charges"]
        + breakdown["sebi"]
        + breakdown["stamp_duty"]
        + breakdown["gst"]
    )
    # total_charges is rounded from unrounded components, so summing the
    # already-rounded components can differ by a cent due to rounding.
    assert breakdown["total_charges"] == pytest.approx(expected_total, abs=0.02)


def test_calculate_option_charges_zero_quantity():
    breakdown = calculate_option_charges(buy_price=100.0, sell_price=110.0, quantity=0)

    assert breakdown == {
        "brokerage": 0.0,
        "stt": 0.0,
        "transaction_charges": 0.0,
        "sebi": 0.0,
        "stamp_duty": 0.0,
        "gst": 0.0,
        "total_charges": 0.0,
    }


def test_estimate_options_round_trip_cost_buy_side_entry():
    breakdown = estimate_options_round_trip_cost(
        entry_side="BUY",
        entry_price=151.30,
        exit_price=156.35,
        quantity=65,
    )

    charges = calculate_option_charges(buy_price=151.30, sell_price=156.35, quantity=65)

    assert breakdown.brokerage == pytest.approx(charges["brokerage"])
    assert breakdown.stt == pytest.approx(charges["stt"])
    assert breakdown.exchange_txn == pytest.approx(charges["transaction_charges"])
    assert breakdown.sebi == pytest.approx(charges["sebi"])
    assert breakdown.stamp == pytest.approx(charges["stamp_duty"])
    assert breakdown.gst == pytest.approx(charges["gst"])
    assert breakdown.total == pytest.approx(charges["total_charges"], abs=0.02)


def test_estimate_options_round_trip_cost_sell_side_entry():
    # Option writing: entry premium received (sell-side leg) is higher than the
    # exit premium paid (buy-side leg) to close the position.
    breakdown = estimate_options_round_trip_cost(
        entry_side="SELL",
        entry_price=156.35,
        exit_price=151.30,
        quantity=65,
    )

    charges = calculate_option_charges(buy_price=151.30, sell_price=156.35, quantity=65)

    assert breakdown.stt == pytest.approx(charges["stt"])
    assert breakdown.stamp == pytest.approx(charges["stamp_duty"])
    assert breakdown.total == pytest.approx(charges["total_charges"], abs=0.02)
