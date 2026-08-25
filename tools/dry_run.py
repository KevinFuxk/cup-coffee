"""
dry_run.py — STREAMING (live-loop) dry-run: prove the detector has NO look-ahead
================================================================================
The backtest scans a whole day at once; LIVE, bars arrive one at a time and you must decide on
each CLOSED bar without seeing the future. This is the bridge test on the staircase (GOING_LIVE
§1, §9): replay the busiest real signal-days bar by bar — at each bar i, run the detector on
ONLY bars[:i+1] (everything known so far) — and check every backtest signal re-appears at the
SAME entry bar, needing no future bars.

  match   : streaming fired the entry at i == breakout_idx (the bar it closed) -> look-ahead-free ✓
  lag +k  : streaming only saw it k bars later (batch peeked at future bars)   -> look-ahead ⚠️
  missed  : batch found it, streaming never did                                -> look-ahead ⚠️

SIGNALS ONLY — places no orders, touches no broker, downloads nothing (reads cached bars).
    python dry_run.py [n_days=8]
"""
from __future__ import annotations
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)
import os, sys, json
from collections import defaultdict
from dataclasses import replace
from datetime import date as Date

from research_data import ResearchData
from pattern_detector import PatternDetector
from cup_coffee_config_v2 import CONFIG


def window(b, m):
    """The 'bars known so far' slice: the first m bars, same symbol/day/timeframe."""
    return replace(b, ts=b.ts[:m], o=b.o[:m], h=b.h[:m], l=b.l[:m], c=b.c[:m], v=b.v[:m])


def main():
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    det = PatternDetector(CONFIG)
    rd = ResearchData(os.environ["POLYGON_API_KEY"])
    events = [json.loads(l) for l in open("data/events.jsonl") if l.strip()]

    # pick the (symbol, day, timeframe) combos with the MOST signals -> most entries tested per replay
    by_day = defaultdict(list)
    for e in events:
        by_day[(e["symbol"], e["day"], e["timeframe"])].append(e)
    combos = sorted(by_day, key=lambda k: -len(by_day[k]))[:n_days]

    tot_match = tot_lag = tot_missed = 0
    for (sym, day, tf) in combos:
        b = rd.bars(sym, Date.fromisoformat(day), tf)
        if b is None:
            print(f"  {sym} {day} {tf}: bars missing in cache, skip"); continue
        D = Date.fromisoformat(day)
        # signals_only on BOTH sides -> compares the pure live signal logic (no labeling artifact)
        batch = {ev.breakout_idx: ev for ev in det.detect(b, sym, D, signals_only=True)}   # whole-day scan

        # LIVE LOOP: grow the window one bar at a time; note the FIRST bar each entry appears at
        first_fire = {}
        start = det.cup_min + det.h_min + 2
        for i in range(start, len(b)):
            for ev in det.detect(window(b, i + 1), sym, D, signals_only=True):
                first_fire.setdefault(ev.breakout_idx, i)

        match = lag = missed = 0; notes = []
        for k in sorted(batch):
            if k in first_fire:
                d = first_fire[k] - k
                if d == 0:
                    match += 1
                else:
                    lag += 1; notes.append(f"entry@{k} LAG +{d}")
            else:
                missed += 1; notes.append(f"entry@{k} MISSED")
        tot_match += match; tot_lag += lag; tot_missed += missed
        flag = "✅" if (lag == 0 and missed == 0) else "⚠️ "
        print(f"{flag} {sym.split(':')[-1]:7} {day} {tf:4} {len(b):3}bars  "
              f"batch={len(batch):2}  match={match} lag={lag} missed={missed}"
              + (("   " + "; ".join(notes[:3])) if notes else ""))

    print(f"\nTOTAL over {len(combos)} signal-days:  match={tot_match}  lag={tot_lag}  missed={tot_missed}")
    if tot_lag == 0 and tot_missed == 0 and tot_match:
        print("✅ NO LOOK-AHEAD — every backtest signal fires live at its exact entry bar, from closed past bars only.")
        print("   The detector is safe to run forward on a live feed (signals only). Next rung: the safety layer.")
    else:
        print("⚠️  LOOK-AHEAD FOUND — some signals need future bars. Fix the detector before any paper order.")


if __name__ == "__main__":
    main()
