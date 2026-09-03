"""
cache_15s.py — accumulate the private 15-second story-stock dataset
====================================================================
Run every evening (replay_record.py calls it automatically). For each symbol
on the day's watchlist it pulls the completed session's 15-second bars from
IBKR and saves them to cache/ibkr15s/<SYM>/<YYYY-MM-DD>.json (same row schema
as cache/ibkr5: {"t": epoch_ms, "o","h","l","c","v"}).

WHY THIS EXISTS (pivot 2026-09-02): no vendor sells 15s history for the
manually-picked news universe going back years — but IBKR serves ~200 days
back, and every day we cache is a day the future backtest owns forever.
Idempotent: existing (symbol, day) files are skipped.

    python cache_15s.py                 # today's watchlist symbols, latest session
    python cache_15s.py MRNA,NVDA       # explicit symbols
"""
from __future__ import annotations

import glob
import json
import os
import sys
from zoneinfo import ZoneInfo

from live_trader_ibkr import read_watchlist

ET = ZoneInfo("America/New_York")
OUT = "cache/ibkr15s"


def latest_archived_watchlist() -> str | None:
    files = sorted(glob.glob("data/watchlists/*.txt"))
    return files[-1] if files else None


def main() -> None:
    args = [a for a in sys.argv[1:]]
    day_arg = next((a for a in args if a[:4].isdigit()), None)
    cli = next((a for a in args if not a[:4].isdigit()), None)
    src = latest_archived_watchlist()
    syms, where = read_watchlist(cli, src or "auto")
    print(f"caching 15s bars for {len(syms)} symbol(s) from {where}")

    try:
        from ib_async import IB, Stock
    except ImportError:
        sys.exit("✗ ib_async not installed")
    ib = IB()
    try:
        ib.connect("127.0.0.1", 4002, clientId=52, timeout=10)
    except Exception as e:
        sys.exit(f"✗ can't reach IB Gateway on 4002  ({e})")
    ib.reqMarketDataType(1)

    saved = skipped = failed = 0
    for sym in syms:
        if day_arg and os.path.exists(f"{OUT}/{sym}/{day_arg}.json"):
            skipped += 1                               # already on disk: no IBKR pull at all
            continue
        c = Stock(sym, "SMART", "USD")
        try:
            ib.qualifyContracts(c)
            if not getattr(c, "conId", 0):
                raise ValueError("unknown contract")
            bl = ib.reqHistoricalData(c, endDateTime="", durationStr="1 D",
                                      barSizeSetting="15 secs", whatToShow="TRADES",
                                      useRTH=True, formatDate=2, keepUpToDate=False)
        except Exception as e:
            print(f"  {sym}: FAILED ({e})")
            failed += 1
            continue
        if not bl:
            print(f"  {sym}: no bars")
            failed += 1
            continue
        day = max(x.date.astimezone(ET).date() for x in bl).isoformat()
        path = f"{OUT}/{sym}/{day}.json"
        if os.path.exists(path):
            skipped += 1
            continue
        os.makedirs(f"{OUT}/{sym}", exist_ok=True)
        rows = [{"t": int(x.date.timestamp() * 1000), "o": float(x.open), "h": float(x.high),
                 "l": float(x.low), "c": float(x.close), "v": float(x.volume)}
                for x in bl if x.date.astimezone(ET).date().isoformat() == day]
        json.dump(rows, open(path, "w"))
        print(f"  {sym}: {len(rows)} bars -> {path}")
        saved += 1
        ib.sleep(1)                                    # IBKR pacing
    ib.disconnect()
    print(f"done: {saved} saved, {skipped} already cached, {failed} failed")


if __name__ == "__main__":
    main()
