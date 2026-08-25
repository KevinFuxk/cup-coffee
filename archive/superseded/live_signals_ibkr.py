"""
live_signals_ibkr.py — LIVE dry-run on IBKR FULL-COVERAGE data, SIGNALS ONLY (no orders)
========================================================================================
Twin of live_signals.py, but bars come from Interactive Brokers — the full consolidated tape
(NYSE / AMEX / Nasdaq), NOT the ~2-3% IEX slice — so live signals match the backtest far better
on thin gappers. Same look-ahead-free detector, same Bars accumulation. Places NOTHING.

Your side (setup):
  * IB Gateway or TWS running in PAPER mode, API enabled
    (Global Config > API > Settings > Enable ActiveX and Socket Clients; note the socket port).
  * pip install ib_async
  * DATA: free 15-min DELAYED needs no subscription (use --delayed to test the pipe). For REAL-TIME
    full coverage, subscribe on your LIVE account to "US Securities Snapshot & Futures Value Bundle"
    ($10/mo, waivable) + "US Equity and Options Add-On Streaming Bundle" ($4.50/mo), and tick
    "share market data with paper trading account" in Account Settings.

We take only CLOSED bars (the last, still-forming bar is always skipped) — same no-look-ahead rule
the dry-run proved. On connect we seed today's bars so setups already in progress show immediately.

    python live_signals_ibkr.py --delayed                 # FREE delayed data — test the pipe, no sub
    python live_signals_ibkr.py NVDA,AMD,SMCI             # real-time (needs the subscription), TWS paper
    python live_signals_ibkr.py NVDA,AMD --port 4002     # IB Gateway paper port
"""
from __future__ import annotations
import sys, argparse
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
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497, help="TWS paper 7497 | IB Gateway paper 4002")
    ap.add_argument("--client-id", type=int, default=7)
    ap.add_argument("--delayed", action="store_true", help="free 15-min delayed data (no subscription needed)")
    a = ap.parse_args()
    syms = a.symbols.split(",")

    try:
        from ib_async import IB, Stock
    except ImportError:
        sys.exit("✗ ib_async not installed.  ->  pip install ib_async")

    det = PatternDetector(CONFIG)
    store: dict[str, Bars] = {}
    fired: dict[str, set] = defaultdict(set)

    def ingest(sym: str, bl, report: bool):
        """Append every CLOSED bar (all but the last, forming one) not yet seen; report NEW signals."""
        B = store.get(sym)
        for b in bl[:-1]:                                   # skip bl[-1] — it's still forming (no look-ahead)
            t = b.date.astimezone(ET).replace(tzinfo=None) if hasattr(b.date, "astimezone") else b.date
            if not (dtime(9, 30) <= t.time() <= dtime(16, 0)):
                continue
            if B is None or B.date != t.date():
                B = Bars(symbol=sym, date=t.date(), timeframe="1min", ts=[], o=[], h=[], l=[], c=[], v=[])
                store[sym] = B; fired[sym] = set()
            if B.ts and t <= B.ts[-1]:                      # already ingested this bar
                continue
            B.ts.append(t); B.o.append(float(b.open)); B.h.append(float(b.high))
            B.l.append(float(b.low)); B.c.append(float(b.close)); B.v.append(float(b.volume))
            for e in det.detect(B, sym, B.date, signals_only=True):
                if e.breakout_idx in fired[sym]:
                    continue
                fired[sym].add(e.breakout_idx)
                if report:
                    rps = e.entry_price - e.stop_price
                    shares = int((0.01 * 1000) / rps) if rps > 0 else 0
                    print(f"🔔 {t:%H:%M} {sym:6} arm BUY-STOP ${e.entry_price:.2f}  stop ${e.stop_price:.2f}  "
                          f"risk ${rps:.2f}/sh  (~{shares} sh @ $1k/1%)   [no order]")

    ib = IB()
    try:
        ib.connect(a.host, a.port, clientId=a.client_id, timeout=10)
    except Exception as e:
        sys.exit(f"✗ can't reach TWS/Gateway at {a.host}:{a.port} — running? API enabled? port right?  ({e})")
    ib.reqMarketDataType(3 if a.delayed else 1)             # 1 = real-time, 3 = 15-min delayed

    print(f"LIVE signals — IBKR {'DELAYED (free)' if a.delayed else 'real-time'} — SIGNALS ONLY, no orders")
    print(f"  watching: {', '.join(syms)}\n")

    def on_update(bars, has_new_bar):
        if has_new_bar:                                     # a bar just closed (a new one started)
            ingest(bars.contract.symbol, bars, report=True)

    for s in syms:
        bl = ib.reqHistoricalData(Stock(s, "SMART", "USD"), endDateTime="", durationStr="7200 S",
                                  barSizeSetting="1 min", whatToShow="TRADES", useRTH=True,
                                  formatDate=2, keepUpToDate=True)
        ingest(s, bl, report=False)                         # seed today's history silently (mark prior setups seen)
        bl.updateEvent += on_update
        print(f"  {s}: seeded {len(bl)} bars")
    print("\nstreaming…  (Ctrl-C to stop)\n")
    ib.run()


if __name__ == "__main__":
    main()
