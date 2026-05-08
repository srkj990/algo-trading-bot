#!/usr/bin/env python3
"""
Preflight live check for intraday-options runtime.
Tests Kite auth, instruments cache, underlying fetch, ATM contract resolution, basic quote path.
Run before market hours: python preflight.py
"""

import sys
import logging
from datetime import datetime
from pathlib import Path

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('logs/preflight.log')
    ]
)
logger = logging.getLogger(__name__)

Path('logs').mkdir(exist_ok=True)

def test_kite_auth():
    """Test Kite authentication via client initialization."""
    logger.info("=== 1. Testing Kite Auth ===")
    try:
        from brokers.clients import KiteBrokerClient
        client = KiteBrokerClient()
        # Trigger auth by calling a read-only method
        positions = client.get_positions()
        logger.info(f"[OK] Auth OK: {len(positions)} positions loaded")
        return True
    except Exception as e:
        logger.error(f"[FAILED] Auth FAILED: {e}")
        logger.info("  Run `python auto_auth.py` to refresh KITE_ACCESS_TOKEN")
        return False

def test_instruments_cache():
    """Test NFO instruments cache availability."""
    logger.info("=== 2. Testing Instruments Cache (NFO) ===")
    try:
        from network_utils import get_cached_kite_instruments
        instruments = get_cached_kite_instruments("NFO", lambda: None)
        logger.info(f"[OK] Cache OK: {len(instruments):,} NFO instruments")
        return True
    except Exception as e:
        logger.error(f"[FAILED] Cache FAILED: {e}")
        logger.info("  Check internet/Kite API status")
        return False

def test_underlying_fetch():
    """Test NIFTY spot price fetch."""
    logger.info("=== 3. Testing Underlying Fetch (NIFTY) ===")
    try:
        from fno_data_fetcher import get_underlying_spot_price
        spot = get_underlying_spot_price("NIFTY")
        logger.info(f"[OK] NIFTY spot: Rs{spot:,.2f}")
        return True
    except Exception as e:
        logger.error(f"[FAILED] Underlying FAILED: {e}")
        return False

def test_atm_resolution():
    """Test ATM strike resolution for next NIFTY expiry."""
    logger.info("=== 4. Testing ATM Contract Resolution ===")
    try:
        from fno_data_fetcher import (
            get_available_expiries, get_atm_option_strike, get_fno_derivatives_exchange
        )
        expiries = get_available_expiries("NIFTY")
        if not expiries:
            raise RuntimeError("No NIFTY expiries found")
        next_expiry = expiries[0]
        exchange = get_fno_derivatives_exchange("NIFTY")
        atm_strike = get_atm_option_strike("NIFTY", next_expiry)
        logger.info(f"[OK] ATM OK: {next_expiry} strike {atm_strike} (exchange: {exchange})")
        return True
    except Exception as e:
        logger.error(f"[FAILED] ATM FAILED: {e}")
        return False

def test_quote_path():
    """Test quotes for NIFTY spot, ATM CE/PE."""
    logger.info("=== 5. Testing Quote Path ===")
    try:
        from fno_data_fetcher import (
            get_available_expiries, get_atm_option_strike, resolve_option_contract,
            get_fno_derivatives_exchange
        )
        from brokers.clients import KiteBrokerClient
        
        client = KiteBrokerClient()
        spot_symbol = "NSE:NIFTY 50"
        spot_quote = client.get_quote(spot_symbol)
        logger.info(f"[OK] Spot quote OK: NIFTY {spot_quote.last_price:.2f}")
        
        expiries = get_available_expiries("NIFTY")
        if not expiries:
            raise RuntimeError("No expiries for quote test")
        next_expiry = expiries[0]
        exchange = get_fno_derivatives_exchange("NIFTY")
        atm_strike = get_atm_option_strike("NIFTY", next_expiry)
        ce_symbol = resolve_option_contract("NIFTY", next_expiry, atm_strike, "CE")
        pe_symbol = resolve_option_contract("NIFTY", next_expiry, atm_strike, "PE")
        
        ce_quote = client.get_quote(ce_symbol)
        pe_quote = client.get_quote(pe_symbol)
        logger.info(f"[OK] ATM Quotes OK: CE {ce_symbol} Rs{ce_quote.last_price:.2f}, PE {pe_symbol} Rs{pe_quote.last_price:.2f}")
        return True
    except Exception as e:
        logger.error(f"[FAILED] Quotes FAILED: {e}")
        return False

def main():
    start_time = datetime.now()
    logger.info(f"[START] Preflight check started at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    tests = [
        test_kite_auth,
        test_instruments_cache,
        test_underlying_fetch,
        test_atm_resolution,
        test_quote_path,
    ]
    
    results = {}
    for test in tests:
        name = test.__name__.replace('_', ' ').replace('test ', '').title()
        success = test()
        results[name] = success
    
    passed = sum(results.values())
    total = len(results)
    
    logger.info("\n=== SUMMARY ===")
    logger.info(f"{passed}/{total} tests passed")
    if passed == total:
        logger.info("[CLEAR] ALL CLEAR - Ready for live trading!")
    else:
        logger.info("[WARNING] Fix issues above before trading.")
    
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    logger.info(f"Completed in {duration:.1f}s")
    
    sys.exit(0 if passed == total else 1)

if __name__ == "__main__":
    main()

