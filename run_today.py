"""
run_today.py — run the LIVE pipeline on real data for one trading day
=====================================================================
This is the "does it actually work with my keys" script. It scans the most
recent trading day, pulls the real charts, finds cup-and-handles, and appends
them to events.jsonl.

Run it from a terminal AFTER setting your keys (the `export` lines in ~/.zshrc):
    python run_today.py

Honest scope: this uses TradingView's CURRENT snapshot, which is correct for
the most recent session (live/daily use). It is NOT valid for deep history —
TradingView only knows "today". Building a full YEAR of history needs a
point-in-time universe (Polygon grouped-daily + fundamentals), which is the
next piece to build.
"""

from __future__ import annotations

import os
import logging
from datetime import date, timedelta

from cup_coffee_config_v2 import CONFIG
from providers_real import (TradingViewUniverseProvider, PolygonBarProvider,
                            fmp_had_earnings, polygon_energy_beta)
from backfill import run_backfill

logging.basicConfig(level=logging.INFO, format="%(message)s")

# keys come from the environment (set in ~/.zshrc) — never hardcode them here
POLY = os.environ["POLYGON_API_KEY"]
FMP  = os.environ["FMP_API_KEY"]


def most_recent_trading_day() -> date:
    d = date.today()
    while d.weekday() >= 5:          # step Sat/Sun back to Friday
        d -= timedelta(days=1)
    return d


if __name__ == "__main__":
    day = most_recent_trading_day()
    print(f"Running live pipeline for {day}\n")

    universe_provider = TradingViewUniverseProvider(
        had_earnings_fn=fmp_had_earnings(FMP),
        energy_beta_fn=polygon_energy_beta(POLY),
    )
    bar_provider = PolygonBarProvider(POLY)

    # run the (already-tested) chain for a single day
    totals = run_backfill(
        config=CONFIG,
        uni_provider=universe_provider,
        bar_provider=bar_provider,
        start=day, end=day,
        out_path="events.jsonl",
        checkpoint_path="live.checkpoint",
        sleep_between=0.3,            # gentle on the API; free Polygon tier needs ~13s instead
    )

    print(f"\nDone. Stored {totals['events']} cup-and-handle events "
          f"(W {totals['wins']} / L {totals['losses']} / T {totals['timeouts']}) in events.jsonl")
