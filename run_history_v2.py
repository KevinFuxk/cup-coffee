"""
run_history_v2.py — rebuild the 4-year pile on the NEW spec
===========================================================
New detector (cup 15-60, handle 4-50 + ratchet, rim-line rule, entry = rim+$0.01,
stop = handle low, 3:49pm exit) + earnings-gapper universe (momentum per-tier OR
rev-growth >=40% & gap >=10%, via FMP earnings dates + Polygon revenue) + cross-
timeframe dedup (1>2>5min within 5 min). Charts are cached; FMP is retried/cached.

Writes a FRESH events.jsonl (the launcher backs up the old one). Background, hours.
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
FMP = os.environ.get("FMP_API_KEY")


def build_providers():
    rd = ResearchData(POLY, FMP)
    def had_earn(s, d):
        t = s.split(":")[-1]
        return t in rd.earnings_symbols(d) or t in rd.earnings_symbols(d - timedelta(days=1))
    def rev(s, d):
        return rd.revenue_growth_yoy(s, d) if had_earn(s, d) else None
    uni = PolygonUniverseProvider(POLY, had_earnings_fn=had_earn, energy_beta_fn=None, revenue_fn=rev)
    return uni, CachedBarProvider(rd)


if __name__ == "__main__":
    uni, bars = build_providers()
    end = date.today()
    start = end - timedelta(days=1440)            # ~3.95 years
    print(f"v2 backfill {start} -> {end}  (new detector + earnings universe + dedup)\n")
    totals = run_backfill(CONFIG, uni, bars, start, end,
                          out_path="events.jsonl", checkpoint_path="backfill.checkpoint",
                          sleep_between=0.0)
    print(f"\nDone: {totals['events']} events / {totals['days']} days "
          f"(W {totals['wins']} / L {totals['losses']} / T {totals['timeouts']}).")
