"""
run_history_4y.py — rebuild the pile over ~4 years, caching every chart
=======================================================================
Differences from run_history.py:
  * ~4-year lookback (the max Polygon Starter serves) -> ~2x the trades.
  * Bars go through the DISK CACHE (research_data.CachedBarProvider), so this run
    also fills cache/minute — re-mining and future strategies then run in minutes.
  * energy_beta GATE is OFF for the research history (energy_beta_fn=None): we keep
    every name and study energy beta as a FACTOR instead (it's a live-trading gate,
    not a research filter). This also removes the slowest per-name calls.

Writes a FRESH events.jsonl (the prior 2-year file is backed up by the launcher).
Resumable via backfill.checkpoint. Background, ~hours.
"""
from __future__ import annotations
import os, logging
from datetime import date, timedelta
from cup_coffee_config_v2 import CONFIG
from providers_historical import PolygonUniverseProvider
from research_data import ResearchData, CachedBarProvider
from backfill import run_backfill

logging.basicConfig(level=logging.INFO, format="%(message)s")
POLY = os.environ["POLYGON_API_KEY"]

if __name__ == "__main__":
    uni = PolygonUniverseProvider(api_key=POLY, had_earnings_fn=None,
                                  energy_beta_fn=None, revenue_fn=None)
    bars = CachedBarProvider(ResearchData(POLY))
    end = date.today()
    start = end - timedelta(days=1440)            # ~3.95 years, inside the entitlement edge
    print(f"4-YEAR backfill {start} -> {end}  (cached charts; energy gate off; resumable)\n")
    totals = run_backfill(CONFIG, uni, bars, start, end,
                          out_path="events.jsonl", checkpoint_path="backfill.checkpoint",
                          sleep_between=0.03)
    print(f"\nDone: {totals['events']} events across {totals['days']} days "
          f"(W {totals['wins']} / L {totals['losses']} / T {totals['timeouts']}).")
