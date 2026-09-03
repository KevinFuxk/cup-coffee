"""
explain_day.py — "why didn't we trade X today?"
================================================
Walks every cup candidate the detector examined and reports WHICH GATE rejected it,
with counts per reason plus detail on the near-misses (the ones that failed by a hair).

Answers the question the replay can't: the replay shows what the bot DID; this shows
what it CONSIDERED and threw away, and why.

    python explain_day.py                      # watchlist symbols, latest completed session
    python explain_day.py UBER                 # one symbol
    python explain_day.py UBER,SHOP 2026-08-05 # explicit symbols and date

Reads bars from IB Gateway (paper, port 4002). Places no orders, touches no data files.
"""
from __future__ import annotations

import os
import sys
from datetime import date as Date, time as dtime
from zoneinfo import ZoneInfo

from cup_coffee_config_v2 import CONFIG
from data_layer import Bars
from live_trader_ibkr import read_watchlist
from pattern_detector import PatternDetector, _is_peak

ET = ZoneInfo("America/New_York")
MINSTOP = 0.25          # same floor the live bot uses


def gates(det: PatternDetector, b: Bars, minstop: float):
    """Every cup candidate -> (reason, detail). Mirrors the live scanner's gate order exactly."""
    reasons: dict[str, int] = {}
    near: dict[str, str] = {}          # keyed by right rim -> one line per distinct setup
    entries: dict[str, str] = {}       # (several left rims can share one right rim)
    rolls = 0
    n = len(b)

    def bump(r):
        reasons[r] = reasons.get(r, 0) + 1

    for li in range(1, n - 1):
        if not _is_peak(b, li):
            continue
        min_ri = 0
        while True:
            cup = det._find_cup(b, li, min_ri)
            if cup is None:
                bump("no valid right rim (±25% band / rim-line / >=15 bars)")
                break
            bot_i, ri = cup
            cup_low = b.l[bot_i]
            rim, left = b.h[ri], b.h[li]
            depth = rim - cup_low
            if depth <= 0:
                bump("degenerate cup (zero depth)")
                break
            _d = min if det.rim_mode == "min" else max
            lim = det.rim_recov * _d(left - cup_low, rim - cup_low)
            if abs(rim - left) >= lim:
                bump("rim symmetry fail (rims not level enough)")
                break
            cap = det.h_depth_frac * depth
            trig = rim + det.entry_off
            earliest = max(ri + det.h_min - 1, ri + 2)
            hl, outcome = float("inf"), None
            for k in range(ri + 1, min(n, ri + det.h_max) if det.h_max else n):
                if k >= earliest and hl < float("inf") and b.h[k] >= trig:
                    sp = (trig - hl) / trig * 100
                    if sp < minstop:
                        outcome = "stop below the min-stop floor"
                        near[f"ms{ri}"] = (f"    {b.ts[ri]:%H:%M} rim ${rim:.2f} -> would enter "
                                           f"{b.ts[k]:%H:%M} @ ${trig:.2f}, but stop is {sp:.3f}% "
                                           f"(floor {minstop}%) — sub-noise, blocked")
                    else:
                        outcome = "ENTERED"
                        entries[str(ri)] = (f"    {b.ts[k]:%H:%M} entry ${trig:.2f} stop ${hl:.2f} "
                                            f"({sp:.2f}% of price)  cup {b.ts[li]:%H:%M}->{b.ts[ri]:%H:%M}")
                    break
                if det.rim_roll and b.h[k] > rim + 1e-9:
                    outcome, min_ri = "roll", k
                    rolls += 1
                    break
                hl = min(hl, b.l[k])
                if rim - hl > cap:
                    outcome = "handle too deep (> 20% of cup)"
                    ratio = depth / (rim - hl)
                    if ratio >= 3.5:                       # a near miss: 3.5:1 or better, needed 5:1
                        near[f"hd{ri}"] = (f"    {b.ts[ri]:%H:%M} rim ${rim:.2f}: handle dipped "
                                           f"${rim-hl:.2f} vs cap ${cap:.2f} — {ratio:.1f}:1, "
                                           f"needed 5:1 (missed by ${(rim-hl)-cap:.2f})")
                    break
            if outcome == "roll":
                continue
            bump(outcome or "handle never broke out (still forming at the close)")
            break
    return reasons, near, entries, rolls


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    symbols = args[0] if args and not args[0][0].isdigit() else None
    day = next((a for a in args if a[0].isdigit()), None)
    syms, src = read_watchlist(symbols, "auto")   # today's export, never the stale fallback

    try:
        from ib_async import IB, Stock
    except ImportError:
        sys.exit("✗ ib_async not installed")
    det = PatternDetector(CONFIG)
    ib = IB()
    try:
        ib.connect("127.0.0.1", 4002, clientId=51, timeout=10)
    except Exception as e:
        sys.exit(f"✗ can't reach IB Gateway on 4002 — running and logged in?  ({e})")
    ib.reqMarketDataType(1)

    print(f"\nWHY DIDN'T WE TRADE?  — symbols from {src}")
    print(f"gates: rolling rim · 4-bar floor · handle ≤20% of cup · min-stop {MINSTOP}%\n")

    for sym in syms:
        c = Stock(sym, "SMART", "USD")
        cpath = f"cache/ibkr15s/{sym}/{day}.json" if day else None
        if cpath and os.path.exists(cpath):            # SPEED: the evening cache already has the day
            from live_trader_ibkr import cached_day_bars
            bl = cached_day_bars(sym, day)[:-1]
        else:
            try:
                ib.qualifyContracts(c)
                bl = ib.reqHistoricalData(c, endDateTime="", durationStr="2 D", barSizeSetting="15 secs",
                                          whatToShow="TRADES", useRTH=True, formatDate=2, keepUpToDate=False)
            except Exception as e:
                print(f"{sym}: data unavailable ({type(e).__name__})\n")
                continue
        if not bl:
            print(f"{sym}: no bars returned\n")
            continue
        D = Date.fromisoformat(day) if day else max(
            x.date.astimezone(ET).date() if hasattr(x.date, "astimezone") else x.date.date() for x in bl)
        b1 = Bars(symbol=sym, date=D, timeframe="15s", ts=[], o=[], h=[], l=[], c=[], v=[])
        for x in bl:
            t = x.date.astimezone(ET).replace(tzinfo=None) if hasattr(x.date, "astimezone") else x.date
            if t.date() == D and dtime(9, 30) <= t.time() <= dtime(15, 59):
                b1.ts.append(t); b1.o.append(float(x.open)); b1.h.append(float(x.high))
                b1.l.append(float(x.low)); b1.c.append(float(x.close)); b1.v.append(float(x.volume))
        if not len(b1):
            print(f"{sym}: no RTH bars for {D}\n")
            continue

        rng = (max(b1.h) - min(b1.l)) / min(b1.l) * 100
        print(f"── {sym}  {D}  ({len(b1)} 15s bars, {rng:.1f}% intraday range) ──")
        any_entry = False
        for tf, b in (("15s", b1),):                  # THE PROGRAM: 15-second cup-and-handle
            if len(b) < det.cup_min + det.h_min + 2:
                continue
            reasons, near, entries, rolls = gates(det, b, MINSTOP)
            total = sum(reasons.values())
            print(f"  {tf}: {total} cup candidates examined"
                  + (f", {rolls} rim dethrones" if rolls else ""))
            for r, cnt in sorted(reasons.items(), key=lambda kv: -kv[1]):
                mark = "✅" if r == "ENTERED" else "  "
                print(f"     {mark} {cnt:4}  {r}")
            for line in entries.values():
                any_entry = True
                print(line)
            for line in sorted(near.values())[:4]:
                print(line)
        if not any_entry:
            print("  → no trade: every candidate failed a gate above.")
        print()
    ib.disconnect()


if __name__ == "__main__":
    main()
