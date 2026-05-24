from config import get_risk_style_presets, is_intraday_engine_name


def test_intraday_engines_use_intraday_risk_style_presets():
    preset = get_risk_style_presets("intraday_equity")["2"]
    assert preset["name"] == "BALANCED"
    assert preset["atr_stop_multiplier"] == 1.35
    assert preset["trailing_atr_multiplier"] == 0.9
    assert preset["target_risk_reward"] == 1.5


def test_positional_engines_use_positional_risk_style_presets():
    preset = get_risk_style_presets("delivery_equity")["2"]
    assert preset["name"] == "BALANCED"
    assert preset["atr_stop_multiplier"] == 1.65
    assert preset["trailing_atr_multiplier"] == 1.25
    assert preset["target_risk_reward"] == 2.0


def test_intraday_engine_name_detection_matches_supported_engine_set():
    assert is_intraday_engine_name("intraday_futures") is True
    assert is_intraday_engine_name("options_equity") is False
