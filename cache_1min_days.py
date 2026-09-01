"""
cache_1min_days.py — accumulate the private 1-MINUTE story-stock dataset (HTF channel)
=======================================================================================
The high-tight-flag twin of cache_15s.py (same idea, same row schema, different
timeframe and directory). Run every evening — record_tightflag.py calls it
automatically. For each symbol on the day's watchlist it pulls the completed
session's 1-minute bars from IBKR and saves them to

    cache/ibkr1min_days/<SYM>/<YYYY-MM-DD>.json     rows {"t":epoch_ms,"o","h","l","c","v"}

Do NOT confuse with cache/ibkr1min/ or cache/ibkr5/ — those are the OLD
SPY/QQQ research caches from the pre-pivot program (they exist; leave them alone). This directory is the pivot-era dataset and is owned by the HTF channel.

Idempotent: existing (symbol, day) files are skipped. A --day lets the evening
run (or a backfill) target a specific past session — IBKR serves ~200 days of
1-min history, so every watchlist day we record is recoverable for a while and
then ours forever.

    python cache_1min_days.py                       # today's watchlist, latest session
    python cache_1min_days.py --day 2026-08-28      # that day's ARCHIVED watchlist
    python cache_1min_days.py MRNA,NVDA             # explicit symbols, latest session
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import date as Date
from zoneinfo import ZoneInfo

from live_trader_ibkr import read_watchlist          # shared discovery — import only

ET = ZoneInfo("America/New_York")
OUT = "cache/ibkr1min_days"
CLIENT_ID = 20        # daily-program assignment (cup: 8/9/51-53; HTF live 18, record 19)


def watchlist_for(day: str | None) -> tuple[list[str], str]:
    """The archived list for --day, else the newest archive, else auto-discovery."""
    if day:
        p = f"data/watchlists/{day}.txt"
        if not os.path.exists(p):
            raise SystemExit(f"✗ {p} not found — that day's watchlist was never archived")
        return read_watchlist(None, p)
    files = sorted(glob.glob("data/watchlists/*.txt"))
    return read_watchlist(None, files[-1] if files else "auto")


def cache_day(ib, Stock, syms: list[str], day: str | None) -> tuple[int, int, int]:
    """Fetch + store each symbol's completed 1-min session. Returns (saved, skipped, failed)."""
    end = f"{day.replace('-', '')} 23:59:59 US/Eastern" if day else ""
    saved = skipped = failed = 0
    for sym in syms:
        # cheap idempotency first: a --day re-run must not even hit IBKR
        if day and os.path.exists(f"{OUT}/{sym}/{day}.json"):
            skipped += 1
            continue
        c = Stock(sym, "SMART", "USD")
        try:
            ib.qualifyContracts(c)
            if not getattr(c, "conId", 0):
                raise ValueError("unknown contract")
            bl = ib.reqHistoricalData(c, endDateTime=end, durationStr="1 D",
                                      barSizeSetting="1 min", whatToShow="TRADES",
                                      useRTH=True, formatDate=2, keepUpToDate=False)
        except Exception as e:
            print(f"  {sym}: FAILED ({e})")
            failed += 1
            continue
        if not bl:
            print(f"  {sym}: no bars")
            failed += 1
            continue
        got = max(x.date.astimezone(ET).date() for x in bl).isoformat()
        if day and got != day:
            print(f"  {sym}: IBKR returned {got}, wanted {day} — not saved")
            failed += 1
            continue
        path = f"{OUT}/{sym}/{got}.json"
        if os.path.exists(path):
            skipped += 1
            continue
        os.makedirs(f"{OUT}/{sym}", exist_ok=True)
        rows = [{"t": int(x.date.timestamp() * 1000), "o": float(x.open), "h": float(x.high),
                 "l": float(x.low), "c": float(x.close), "v": float(x.volume)}
                for x in bl if x.date.astimezone(ET).date().isoformat() == got]
        json.dump(rows, open(path, "w"))
        print(f"  {sym}: {len(rows)} bars -> {path}")
        saved += 1
        ib.sleep(2)                                    # IBKR pacing
    return saved, skipped, failed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="?", default=None)
    ap.add_argument("--day", default=None, help="YYYY-MM-DD: cache that archived day")
    a = ap.parse_args()
    if a.day:
        Date.fromisoformat(a.day)                      # validate early
    if a.symbols:
        syms, where = read_watchlist(a.symbols, "auto")
    else:
        syms, where = watchlist_for(a.day)
    print(f"caching 1-min bars for {len(syms)} symbol(s) from {where}"
          + (f" (session {a.day})" if a.day else ""))

    try:
        from ib_async import IB, Stock
    except ImportError:
        raise SystemExit("✗ ib_async not installed")
    ib = IB()
    try:
        ib.connect("127.0.0.1", 4002, clientId=CLIENT_ID, timeout=10)
    except Exception as e:
        raise SystemExit(f"✗ can't reach IB Gateway on 4002  ({e})")
    ib.reqMarketDataType(1)
    saved, skipped, failed = cache_day(ib, Stock, syms, a.day)
    ib.disconnect()
    print(f"done: {saved} saved, {skipped} already cached, {failed} failed")


if __name__ == "__main__":
    main()
