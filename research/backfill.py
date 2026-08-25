"""
backfill.py — CUP COFFEE one-time history builder
==================================================
Walks a range of past trading days and runs the full chain for each:

    universe (Stage 1)  ->  bars (Stage 2)  ->  detect+label (Stage 3)
    ->  append every labeled event to events.jsonl

This is what you run ONCE (over ~1 year) to create the starting database the
scorer and loop need. Nothing can be scored until this has run.

Built for a long, real run:
  * RESUMABLE — a checkpoint records the last finished day; re-running continues
    from there, so an interruption (or a rate-limit stall) costs nothing.
  * FAULT-TOLERANT — one bad day is logged and skipped, not fatal.
  * RATE-LIMITED — optional sleep between names to respect API limits.
  * STREAMING WRITE — events are flushed to disk per day (JSON-lines), so the
    file is always valid even if the run stops.

Honesty note for a TRUTHFUL backtest: feed it a POINT-IN-TIME universe provider
that includes delisted names. TradingView's live snapshot is fine for paper/live
but will bias history; for the backtest, source the universe from Polygon
grouped-daily bars + point-in-time fundamentals.

Runs as-is: `python backfill.py` uses the synthetic providers over one week so
you can watch it build the database and print a summary.
"""

from __future__ import annotations
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)

import json
import os
import time
import logging
from dataclasses import asdict
from datetime import date as Date, timedelta

from universe import DailyUniverseBuilder
from data_layer import DataLayer
from pattern_detector import PatternDetector

logger = logging.getLogger("cupcoffee.backfill")


def trading_days(start: Date, end: Date):
    """Weekdays in the range. (For holiday precision, swap in
    pandas_market_calendars; on holidays the bar provider just returns no data.)"""
    d = start
    while d <= end:
        if d.weekday() < 5:                      # Mon-Fri
            yield d
        d += timedelta(days=1)


def _event_record(e, cand, timeframe: str) -> dict:
    rec = asdict(e)
    rec["day"] = e.day.isoformat()               # Date -> str for JSON
    rec["timeframe"] = timeframe
    rec["size_tier"] = cand.size_tier            # carry Stage-1 tags onto the event
    rec["return_bucket"] = cand.return_bucket
    rec["earnings_bucket"] = cand.earnings_bucket
    rec["reason"] = cand.reason                   # momentum_gap | earnings_gap | whitelist
    return rec


def run_backfill(config, uni_provider, bar_provider, start: Date, end: Date,
                 out_path: str = "data/events.jsonl",
                 checkpoint_path: str = "data/backfill.checkpoint",
                 sleep_between: float = 0.0) -> dict:
    # resume from checkpoint
    last_done = None
    if os.path.exists(checkpoint_path):
        last_done = Date.fromisoformat(open(checkpoint_path).read().strip())
        logger.info("Resuming after %s", last_done)

    builder = DailyUniverseBuilder(config, uni_provider)
    layer = DataLayer(config, bar_provider)
    detector = PatternDetector(config)

    totals = {"days": 0, "events": 0, "wins": 0, "losses": 0, "timeouts": 0}
    with open(out_path, "a") as fout:
        for day in trading_days(start, end):
            if last_done and day <= last_done:
                continue
            try:
                wl = builder.build(day)
                day_events = 0
                _RANK = {"1min": 0, "2min": 1, "5min": 2}
                for cand in wl.tradable():
                    series = layer.load(cand.symbol, day)
                    if series is None:
                        continue
                    # collect detections across timeframes, with entry time + tf priority
                    found = []   # (entry_dt, rank, tf, event)
                    for tf, bars in series.bars.items():
                        for e in detector.detect(bars, cand.symbol, day):
                            found.append((bars.ts[e.breakout_idx], _RANK.get(tf, 9), tf, e))
                    # cross-timeframe dedup: keep 1min>2min>5min; drop any whose entry is
                    # within 5 minutes of an already-kept trade (the same setup on another TF)
                    kept = []
                    for entry_dt, rank, tf, e in sorted(found, key=lambda x: (x[1], x[0])):
                        if any(abs((entry_dt - k[0]).total_seconds()) <= 300 for k in kept):
                            continue
                        kept.append((entry_dt, rank, tf, e))
                    for entry_dt, rank, tf, e in kept:
                        fout.write(json.dumps(_event_record(e, cand, tf)) + "\n")
                        day_events += 1
                        totals["events"] += 1
                        totals["wins"]     += (e.outcome == 1)
                        totals["losses"]   += (e.outcome == -1)
                        totals["timeouts"] += (e.outcome == 0)
                    if sleep_between:
                        time.sleep(sleep_between)
                fout.flush()
                open(checkpoint_path, "w").write(day.isoformat())   # checkpoint AFTER the day is written
                totals["days"] += 1
                logger.info("%s  +%d events  (running total %d)", day, day_events, totals["events"])
            except Exception as ex:                                  # one bad day never kills the run
                logger.warning("skip %s: %s", day, ex)

    logger.info("DONE  days=%d  events=%d  (W %d / L %d / T %d)",
                totals["days"], totals["events"],
                totals["wins"], totals["losses"], totals["timeouts"])
    return totals


# ----------------------------------------------------------------------------
# Demo — synthetic providers over one week (real run: swap in providers_real)
# ----------------------------------------------------------------------------

def _demo_config() -> dict:
    return {
        "universe": {
            "whitelist": ["NASDAQ:QQQ", "AMEX:SPY"],
            "scan": {"min_price": 15.0, "min_dollar_volume": 20_000_000},
            "catalyst_by_size": {
                "mega":  {"earnings": {"rev_min": 0.10, "gap_min": 0.05}, "non_earnings": {"gap_min": 0.05}},
                "large": {"earnings": {"rev_min": 0.20, "gap_min": 0.07}, "non_earnings": {"gap_min": 0.10}},
                "mid":   {"earnings": {"rev_min": 0.30, "gap_min": 0.10}, "non_earnings": {"gap_min": 0.15}},
                "small": {"earnings": {"rev_min": 0.40, "gap_min": 0.10}, "non_earnings": {"gap_min": 0.25}},
                "micro": {"earnings": {"rev_min": 0.40, "gap_min": 0.10}, "non_earnings": {"gap_min": 0.25}},
            },
            "exclude_from_trading": {"energy_beta_above": 0.5},
        },
        "energy_beta": {"window_days": 60},
        "data": {"timeframes": ["1min"], "max_gap_bars": 3, "min_bars": 60},
        "pattern": {"cup_min_bars": 15, "cup_max_bars": 120,
                    "cup_depth_min_atr": 1.0, "cup_depth_max_atr": 40.0,
                    "lip_diff_max_frac_of_depth": 0.25, "handle_min_bars": 4,
                    "handle_max_depth_frac": 0.25, "breakout_buffer_atr": 0.1,
                    "max_handles_per_cup": 3},
        "labeling": {"max_hold_bars": 240},
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from universe import _FakeProvider as FakeUniverse
    from data_layer import _SyntheticProvider as SyntheticBars

    # clean start for the demo
    for f in ("data/events.jsonl", "data/backfill.checkpoint"):
        if os.path.exists(f):
            os.remove(f)

    totals = run_backfill(
        config=_demo_config(),
        uni_provider=FakeUniverse(),
        bar_provider=SyntheticBars(),
        start=Date(2026, 5, 25), end=Date(2026, 5, 29),   # one week (Mon-Fri)
        out_path="data/events.jsonl",
    )

    print("\n=== events.jsonl (first 2 rows) ===")
    with open("data/events.jsonl") as f:
        for line in list(f)[:2]:
            r = json.loads(line)
            print(f"  {r['symbol']:<11} {r['day']} {r['timeframe']} "
                  f"handle#{r['handle_num']} outcome={r['outcome']:+d} "
                  f"pnl={r['pnl_R']:+.2f}R tier={r['size_tier']}")
    print(f"\nDatabase now holds {totals['events']} labeled events. "
          f"Re-running resumes from the checkpoint (skips finished days).")
