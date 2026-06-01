"""
run_history.py — build the BACKTEST history (point-in-time, survivorship-correct)
=================================================================================
Walks past trading days with the Polygon grouped-daily universe and fills
events.jsonl with every cup-and-handle that occurred. This is the foundation the
scorer and loop run on.

Run from a terminal with your keys set (~/.zshrc):
    python run_history.py

IMPORTANT before the full year:
  * You need Polygon's Starter plan (~$29/mo, unlimited calls). The free tier
    (5 calls/min) cannot do this volume.
  * TEST A SMALL RANGE FIRST (uncomment the 2-week line) to confirm data flows
    before committing to the full year.
  * It is resumable — safe to stop and restart (checkpoint).
"""

from __future__ import annotations

import os
import logging
from datetime import date, timedelta

from cup_coffee_config_v2 import CONFIG
from providers_historical import PolygonUniverseProvider
from providers_real import PolygonBarProvider, fmp_had_earnings, polygon_energy_beta
from backfill import run_backfill

logging.basicConfig(level=logging.INFO, format="%(message)s")

POLY = os.environ["POLYGON_API_KEY"]
# FMP's earnings-calendar endpoint is a dead legacy endpoint (403 for current keys),
# so the first pass runs earnings-OFF and FMP is unused. Optional read, no hard crash.
FMP  = os.environ.get("FMP_API_KEY")


if __name__ == "__main__":
    universe_provider = PolygonUniverseProvider(
        api_key=POLY,
        had_earnings_fn=None,     # FMP earnings endpoint is dead (legacy/403). First
                                  # pass is pure momentum gappers, so earnings tags stay
                                  # off; re-enable once a working earnings source is wired.
        energy_beta_fn=polygon_energy_beta(POLY),
        revenue_fn=None,          # first pass: pure momentum-gap branch only
    )
    bar_provider = PolygonBarProvider(POLY)

    end = date.today()
    start = end - timedelta(days=730)         # ~2 years (verified within Polygon's history window)
    # start = end - timedelta(days=14)        # <-- UNCOMMENT to test a small range first

    print(f"Backfilling {start} -> {end}  (resumable; Ctrl-C is safe)\n")
    totals = run_backfill(
        config=CONFIG,
        uni_provider=universe_provider,
        bar_provider=bar_provider,
        start=start, end=end,
        out_path="events.jsonl",
        checkpoint_path="backfill.checkpoint",
        sleep_between=0.1,                     # raise if you hit rate limits
    )
    print(f"\nHistory built: {totals['events']} events across {totals['days']} days "
          f"(W {totals['wins']} / L {totals['losses']} / T {totals['timeouts']}).")
