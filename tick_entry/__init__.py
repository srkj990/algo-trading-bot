"""
tick_entry — intra-candle breakout entry system.

Supports three execution modes without any change to calling code:

  LIVE      WebSocket ticks (KiteTicker) with REST-LTP fallback.
            Entry fires the moment live price crosses the breakout level.

  PAPER     Same LTP polling path as LIVE but fills are *simulated*:
            executor.place_order returns a synthetic OrderResult so that
            positions are tracked identically to LIVE mode.

  BACKTEST  No network calls.  The trigger is evaluated against each
            candle's Open / High / Low — fill at the trigger price
            (or candle Open, whichever is more conservative) so the
            simulation reflects "we got in earlier in the candle".

Public API
----------
    from tick_entry import TickEntryManager
    mgr = TickEntryManager(context)
    entered = mgr.run(candidates, cycle_remaining_seconds=50)
"""

from tick_entry.manager import TickEntryManager

__all__ = ["TickEntryManager"]
