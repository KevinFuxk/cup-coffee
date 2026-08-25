"""
run_extend.py — pull the earlier ~1 year the 5-year Polygon plan still allows.

We backfilled 2022-06-29 -> today; the plan actually reaches back ~5 years, so this
grabs the missing slice 2021-06-15 -> 2022-06-28 and APPENDS it to data/events.jsonl.

Uses its OWN checkpoint (data/backfill_extend.checkpoint) so it never collides with the
main backfill checkpoint. Resumable — safe to stop/restart. The harness reaps background
jobs, so run it in YOUR terminal:

    python run_extend.py

(If you ever want to redo it from scratch: delete data/backfill_extend.checkpoint first.)
"""
from __future__ import annotations
import os, logging
from datetime import date
from cup_coffee_config_v2 import CONFIG
from providers_historical import PolygonUniverseProvider
from research_data import ResearchData, CachedBarProvider
from backfill import run_backfill

logging.basicConfig(level=logging.INFO, format="%(message)s")
POLY = os.environ["POLYGON_API_KEY"]


def build_providers():
    rd = ResearchData(POLY)
    uni = PolygonUniverseProvider(POLY, had_earnings_fn=None, energy_beta_fn=None,
                                  revenue_fn=lambda s, d: rd.revenue_growth_yoy(s, d))
    return uni, CachedBarProvider(rd)


if __name__ == "__main__":
    uni, bars = build_providers()
    start = date(2021, 6, 15)        # ~5yr edge of the current plan (authorized as of 2026-06)
    end = date(2022, 6, 28)          # the day before the existing pile starts (2022-06-29)
    print(f"extend backfill {start} -> {end}  (the earlier year the plan allows; "
          f"appends to data/events.jsonl)\n")
    totals = run_backfill(CONFIG, uni, bars, start, end,
                          out_path="data/events.jsonl",
                          checkpoint_path="data/backfill_extend.checkpoint",
                          sleep_between=0.0)
    print(f"\nDone: +{totals['events']} earlier events / {totals['days']} days "
          f"(W {totals['wins']} / L {totals['losses']} / T {totals['timeouts']}).")
