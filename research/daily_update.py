"""
daily_update.py — the AFTER-CLOSE daily routine (runs LOCALLY on your Mac)
==========================================================================
One job, run after the close each trading day:

  1. DETECT the day's cup-and-handles and APPEND them to events.jsonl
     (Stage 1->2->3, on the FROZEN harness: momentum-only, energy gate OFF,
      charts cached) — the research pile grows by one day.
  2. RE-SCORE every factor on that bigger pile, using the SAME fixed ruler
     (full-favorable-path labels, fixed 1R..5R targets) — never changed.
  3. APPEND a dated entry to factor_track.jsonl, so every factor's score is
     recorded over time. Durable edges are the ones whose score holds up
     across many days; a single day means little.

This is LOCAL (not a cloud routine): it needs the project code, the cached
charts, and your POLYGON_API_KEY. Run it after the close:

    python3 daily_update.py            # today's session
    python3 daily_update.py 2025-05-29 # a specific past session (for catch-up/testing)

Schedule it on your Mac (cron / launchd) for ~5pm ET on weekdays; the Mac must
be awake at that time. The fixed ruler is deliberate — see the frozen-harness
note. Only the data grows and the scores update; the methodology does not.
"""

from __future__ import annotations
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)

import os
import sys
import json
import logging
from collections import defaultdict
from datetime import date, timedelta

from cup_coffee_config_v2 import CONFIG
from providers_historical import PolygonUniverseProvider
from research_data import ResearchData, CachedBarProvider
from backfill import run_backfill
import mine_factors

logging.basicConfig(level=logging.INFO, format="%(message)s")
POLY = os.environ["POLYGON_API_KEY"]
TRACK = "data/factor_track.jsonl"


def target_session(argv) -> date:
    if len(argv) > 1:
        return date.fromisoformat(argv[1])
    d = date.today()
    while d.weekday() >= 5:                 # roll weekend back to Friday
        d -= timedelta(days=1)
    return d


def _count(path) -> int:
    return sum(1 for _ in open(path)) if os.path.exists(path) else 0


def _bh(pairs, alpha=0.10):
    m = len(pairs)
    if not m:
        return set()
    ordered = sorted(pairs, key=lambda kp: kp[1])
    sig = set()
    for i, (k, p) in enumerate(ordered, 1):
        if p <= (i / m) * alpha:
            sig = set(k for k, _ in ordered[:i])
    return sig


def main():
    day = target_session(sys.argv)
    print(f"=== DAILY UPDATE — session {day}  (frozen harness) ===\n")

    # 1) detect + append the day's trades (FROZEN settings; charts cached)
    uni = PolygonUniverseProvider(api_key=POLY, had_earnings_fn=None,
                                  energy_beta_fn=None, revenue_fn=None)
    bars = CachedBarProvider(ResearchData(POLY))
    before = _count("data/events.jsonl")
    run_backfill(CONFIG, uni, bars, day, day, out_path="data/events.jsonl",
                 checkpoint_path="data/backfill.checkpoint", sleep_between=0.0)
    total = _count("data/events.jsonl")
    print(f"\n+{total - before} new cup-and-handles on {day}  (pile now {total})\n")

    # skip redundant re-scoring on a no-new-trade day (but always seed the track once)
    if total == before and os.path.exists(TRACK) and _count(TRACK) > 0:
        print("No new trades; factor scores unchanged. Done.")
        return

    # 2) re-score on the SAME fixed ruler (INCREMENTAL: only the new day's trades
    #    get features computed; the rest are reused from mined_table.json)
    mine_factors.main(rebuild=False)

    # 3) append a compact dated entry to the factor track record
    scores = json.load(open(mine_factors.SCORES_PATH))
    real = [s for s in scores if not s["factor"].startswith("noise_")]
    sig = _bh([((s["factor"], s["k"]), s["p"]) for s in real])
    byf = defaultdict(list)
    for s in scores:
        byf[s["factor"]].append(s)
    entry = {"date": day.isoformat(), "total_trades": total, "new_trades": total - before, "factors": {}}
    for f, ss in byf.items():
        best = max(ss, key=lambda s: abs(s["ic"]))
        entry["factors"][f] = {"ic": round(best["ic"], 3), "k": best["k"],
                               "p": round(best["p"], 4),
                               "survives": any((s["factor"], s["k"]) in sig for s in ss)}
    with open(TRACK, "a") as fout:
        fout.write(json.dumps(entry) + "\n")

    survivors = [f for f, v in entry["factors"].items() if v["survives"]]
    print(f"\n=== factor track updated -> {TRACK} ({_count(TRACK)} dated entries) ===")
    print(f"survives multiple-testing today: {', '.join(survivors) if survivors else 'none'}")


if __name__ == "__main__":
    main()
