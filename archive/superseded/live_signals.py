"""
live_signals.py — LIVE dry-run: the detector on a REAL-TIME feed, SIGNALS ONLY (no orders)
==========================================================================================
This is dry_run.py's twin, but on a LIVE stream instead of replayed cached bars. It connects to
Alpaca's FREE real-time IEX feed, accumulates each symbol's 1-min bars as they close, and runs the
SAME look-ahead-free detector (signals_only) on the bars known so far. When a cup-and-handle
completes it prints the buy-stop it WOULD arm — and places NOTHING. This proves the live loop
(stateful, forward-in-time, real clock) reproduces the backtest's signals before any broker order.

Data = Alpaca real-time: free IEX (default) or full-SIP via --feed sip ($99/mo Algo Trader Plus).
IEX is ~2-3% coverage (thin/ghost prices on small gappers); SIP is full coverage = matches the backtest.
Reads APCA_API_KEY_ID / APCA_API_SECRET_KEY from the env (your paper keys). No orders, ever.

    python live_signals.py                       # default liquid names on the free IEX feed
    python live_signals.py NVDA,AMD,SMCI         # point it at the day's gappers
    python live_signals.py NVDA,AMD --feed sip   # full-coverage SIP (needs the $99/mo data plan)
Run during market hours (09:30-16:00 ET). The cup needs ~20+ bars, so nothing fires the first ~20 min.
"""
from __future__ import annotations
import os, sys, argparse
from datetime import time as dtime
from zoneinfo import ZoneInfo
from collections import defaultdict

from pattern_detector import PatternDetector
from cup_coffee_config_v2 import CONFIG
from data_layer import Bars

ET = ZoneInfo("America/New_York")
DEFAULT_SYMS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "AMD", "META"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="?", default=",".join(DEFAULT_SYMS),
                    help="comma-separated tickers (default: a few liquid names for a plumbing test)")
    ap.add_argument("--feed", choices=["iex", "sip"], default="iex",
                    help="iex = free (partial coverage); sip = full coverage ($99/mo Algo Trader Plus)")
    a = ap.parse_args()
    syms = a.symbols.split(",")
    kid = os.environ.get("APCA_API_KEY_ID")
    sec = os.environ.get("APCA_API_SECRET_KEY")
    if not kid or not sec:
        sys.exit("✗ keys not set. Add APCA_API_KEY_ID + APCA_API_SECRET_KEY (paper keys) to ~/.zshrc, then re-run.")
    try:
        from alpaca.data.live import StockDataStream
        from alpaca.data.enums import DataFeed
    except ImportError:
        sys.exit("✗ alpaca-py not installed.  ->  pip install alpaca-py")
    feed = DataFeed.SIP if a.feed == "sip" else DataFeed.IEX

    det = PatternDetector(CONFIG)
    store: dict[str, Bars] = {}
    fired: dict[str, set] = defaultdict(set)

    print(f"LIVE signals — Alpaca {a.feed.upper()} feed — SIGNALS ONLY, no orders")
    print(f"  watching: {', '.join(syms)}")
    print("  (cup needs ~20+ bars to form, so nothing fires in the first ~20 min)\n")

    async def on_bar(bar):
        t = bar.timestamp.astimezone(ET).replace(tzinfo=None)     # UTC -> naive ET (matches the backtest bars)
        if not (dtime(9, 30) <= t.time() <= dtime(16, 0)):
            return                                                # regular trading hours only
        B = store.get(bar.symbol)
        if B is None or B.date != t.date():                       # fresh series at the open / new symbol
            B = Bars(symbol=bar.symbol, date=t.date(), timeframe="1min",
                     ts=[], o=[], h=[], l=[], c=[], v=[])
            store[bar.symbol] = B
            fired[bar.symbol] = set()
        B.ts.append(t); B.o.append(float(bar.open)); B.h.append(float(bar.high))
        B.l.append(float(bar.low)); B.c.append(float(bar.close)); B.v.append(float(bar.volume))

        for e in det.detect(B, bar.symbol, B.date, signals_only=True):   # look-ahead-free path (dry_run proved it)
            if e.breakout_idx in fired[bar.symbol]:
                continue                                          # already reported this setup
            fired[bar.symbol].add(e.breakout_idx)
            rps = e.entry_price - e.stop_price
            shares = int((0.01 * 1000) / rps) if rps > 0 else 0   # example size: $1k acct, 1% risk
            print(f"🔔 {t:%H:%M} {bar.symbol:6} arm BUY-STOP ${e.entry_price:.2f}  "
                  f"stop ${e.stop_price:.2f}  risk ${rps:.2f}/sh  (~{shares} sh @ $1k/1%)   [no order]")

    stream = StockDataStream(kid, sec, feed=feed)                 # iex = free, sip = $99/mo full coverage
    stream.subscribe_bars(on_bar, *syms)
    print("connecting…  (Ctrl-C to stop)\n")
    stream.run()


if __name__ == "__main__":
    main()
