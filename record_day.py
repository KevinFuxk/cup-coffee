"""
record_day.py — turn one archived watchlist day into the program's records
==========================================================================
THE history builder (pivot 2026-09-02). For a day whose source-tagged watchlist
sits in data/watchlists/<day>.txt it:

  1. parses the ###SOURCE sections -> per-ticker source tags + overlap count
     (overlap = named by 2+ trusted sources -> the higher-alpha hypothesis)
  2. UNIVERSE SCREEN with recorded reasons: tradable at IBKR, and open price
     >= $15. Nothing is silently dropped — every ticker gets a row in
     data/universe_log.csv (day, symbol, sources, n_sources, open, qualified,
     reason). The dropped rows are the future small-cap alpha dataset.
  3. pulls the day's 15-SECOND bars for every ticker (cached forever under
     cache/ibkr15s/ — each recorded day enlarges the private dataset)
  4. runs the 15s cup-and-handle on the QUALIFIED names and writes ledger rows
     to data/replay_trades.csv in the exact live-replay schema, both variants
     (minstop=0.25 and 0), idempotent per (session, variant).

Trade semantics match the live shadow exactly: entry = max(trigger, entry-bar
open) (gap-over aware), exits checked from the bar AFTER entry, stop before
target, TP at trigger+6R, EOD at the first bar >= 15:49; R measured against
trigger-stop; peak_R = MFE vs entry. One trade per symbol at a time.

    python record_day.py 2026-08-28
    python record_day.py 2026-08-31
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date as Date, time as dtime
from zoneinfo import ZoneInfo

from cup_coffee_config_v2 import CONFIG
from data_layer import Bars
from pattern_detector import PatternDetector

ET = ZoneInfo("America/New_York")
UNIVERSE_LOG = "data/universe_log.csv"
LEDGER = "data/replay_trades.csv"
MIN_PRICE = 15.0                      # the universe floor, applied at the open
TP_R = 6.0
EOD = dtime(15, 49)


def parse_sources(path: str) -> dict[str, list[str]]:
    """###SECTION-tagged watchlist -> {symbol: [sources]}. Order-preserving."""
    tags: dict[str, list[str]] = {}
    section = "UNTAGGED"
    for field in open(path).read().replace("\n", ",").split(","):
        field = field.strip()
        if not field:
            continue
        if field.startswith("###"):
            section = field[3:].strip().title() or "Untagged"
            continue
        sym = field.upper().split(":")[-1]
        if sym and len(sym) <= 6 and sym.replace(".", "").replace("-", "").isalnum():
            tags.setdefault(sym, [])
            if section not in tags[sym]:
                tags[sym].append(section)
    return tags


def bars_15s(ib, Stock, sym: str, day: Date):
    """The day's 15s bars — from cache/ibkr15s if recorded before, else IBKR (then cached)."""
    path = f"cache/ibkr15s/{sym}/{day}.json"
    if os.path.exists(path):
        rows = json.load(open(path))
    else:
        c = Stock(sym, "SMART", "USD")
        ib.qualifyContracts(c)
        if not getattr(c, "conId", 0):
            return None, "not a tradable US stock at IBKR"
        end = f"{day:%Y%m%d} 23:59:59 US/Eastern"
        bl = ib.reqHistoricalData(c, endDateTime=end, durationStr="1 D",
                                  barSizeSetting="15 secs", whatToShow="TRADES",
                                  useRTH=True, formatDate=2, keepUpToDate=False)
        ib.sleep(1.5)                                    # IBKR pacing
        rows = [{"t": int(x.date.timestamp() * 1000), "o": float(x.open), "h": float(x.high),
                 "l": float(x.low), "c": float(x.close), "v": float(x.volume)}
                for x in bl
                if x.date.astimezone(ET).date() == day]
        if rows:
            os.makedirs(f"cache/ibkr15s/{sym}", exist_ok=True)
            json.dump(rows, open(path, "w"))
    if not rows:
        return None, "no bars for that session (halted / not yet listed / wrong day)"
    from datetime import datetime
    b = Bars(symbol=sym, date=day, timeframe="15s", ts=[], o=[], h=[], l=[], c=[], v=[])
    for r in rows:
        t = datetime.fromtimestamp(r["t"] / 1000, tz=ET).replace(tzinfo=None)
        if dtime(9, 30) <= t.time() <= dtime(15, 59):
            b.ts.append(t); b.o.append(r["o"]); b.h.append(r["h"])
            b.l.append(r["l"]); b.c.append(r["c"]); b.v.append(r["v"])
    return (b, None) if len(b) else (None, "no RTH bars")


def walk_trades(det: PatternDetector, b: Bars, minstop: float) -> list[dict]:
    """Detector events -> ledger trades, live-shadow semantics, one at a time per symbol."""
    out, busy_until = [], -1
    for e in det.detect(b, b.symbol, b.date, signals_only=True):
        i = e.breakout_idx
        if i <= busy_until:
            continue                                   # symbol occupied by the previous trade
        trig, stop = round(e.entry_price, 2), round(e.stop_price, 2)
        risk = trig - stop
        if risk <= 0 or (minstop > 0 and risk / trig * 100 < minstop):
            continue
        entry = max(trig, b.o[i])                      # gap-over aware, like the live shadow
        target = round(trig + TP_R * risk, 2)
        hi, exit_kind, exit_px, exit_i = entry, "EOD", b.c[-1], len(b) - 1
        for j in range(i + 1, len(b)):
            hi = max(hi, b.h[j])
            if b.l[j] <= stop:                         # stop first, always
                exit_kind, exit_px, exit_i = "STOP", stop, j
                break
            if b.h[j] >= target:
                exit_kind, exit_px, exit_i = "TP", target, j
                break
            if b.ts[j].time() >= EOD:
                exit_kind, exit_px, exit_i = "EOD", b.c[j], j
                break
        busy_until = exit_i
        out.append({
            "symbol": b.symbol, "tf": "15s",
            "entry_time": f"{b.ts[i]:%H:%M}", "entry": f"{entry:.2f}",
            "trigger": f"{trig:.2f}", "stop": f"{stop:.2f}",
            "stop_pct": f"{risk / trig * 100:.2f}",
            "exit_time": f"{b.ts[exit_i]:%H:%M}", "exit_kind": exit_kind,
            "exit": f"{exit_px:.2f}", "R": f"{(exit_px - entry) / risk:.2f}",
            "peak_R": f"{(hi - entry) / risk:.2f}",
        })
    return out


def append_ledger(day: str, variant: str, trades: list[dict]) -> None:
    cols = ["session", "variant", "symbol", "tf", "entry_time", "entry", "trigger", "stop",
            "stop_pct", "exit_time", "exit_kind", "exit", "R", "peak_R"]
    old = []
    if os.path.exists(LEDGER):
        old = [r for r in csv.DictReader(open(LEDGER))
               if not (r.get("session") == day and r.get("variant") == variant)]
    for t in trades:
        old.append({"session": day, "variant": variant, **t})
    with open(LEDGER, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", restval="")
        w.writeheader()
        for r in sorted(old, key=lambda x: (x["session"], x["variant"], x["exit_time"])):
            w.writerow(r)


def append_universe(day: str, rows: list[dict]) -> None:
    cols = ["day", "symbol", "sources", "n_sources", "open", "qualified", "reason"]
    old = []
    if os.path.exists(UNIVERSE_LOG):
        old = [r for r in csv.DictReader(open(UNIVERSE_LOG)) if r.get("day") != day]
    with open(UNIVERSE_LOG, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(old + rows, key=lambda x: (x["day"], x["symbol"])):
            w.writerow(r)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python record_day.py YYYY-MM-DD   (needs data/watchlists/<day>.txt)")
    day_s = sys.argv[1]
    day = Date.fromisoformat(day_s)
    wl = f"data/watchlists/{day_s}.txt"
    if not os.path.exists(wl):
        sys.exit(f"✗ {wl} not found — archive the day's source-tagged list first")
    tags = parse_sources(wl)
    overlaps = sorted(s for s, src in tags.items() if len(src) >= 2)
    print(f"{day_s}: {len(tags)} tickers from {wl}")
    print(f"  overlap (2+ sources — the higher-alpha hypothesis): {', '.join(overlaps) or 'none'}")

    from ib_async import IB, Stock
    ib = IB()
    try:
        ib.connect("127.0.0.1", 4002, clientId=53, timeout=10)
    except Exception as e:
        sys.exit(f"✗ can't reach IB Gateway on 4002  ({e})")
    ib.reqMarketDataType(1)

    urows, qualified = [], {}
    for sym, srcs in tags.items():
        b, why = bars_15s(ib, Stock, sym, day)
        opn = b.o[0] if b else None
        if b is None:
            q, reason = "no", why
        elif opn < MIN_PRICE:
            q, reason = "no", f"open ${opn:.2f} < ${MIN_PRICE:.0f} floor (small-cap file)"
        else:
            q, reason = "yes", ""
            qualified[sym] = b
        urows.append({"day": day_s, "symbol": sym, "sources": "+".join(srcs),
                      "n_sources": len(srcs), "open": f"{opn:.2f}" if opn else "",
                      "qualified": q, "reason": reason})
        mark = "✅" if q == "yes" else "🚫"
        print(f"  {mark} {sym:<6} [{'+'.join(srcs)}]" + (f"  open {opn:.2f}" if opn else "")
              + (f"  — {reason}" if reason else ""))
    ib.disconnect()
    append_universe(day_s, urows)

    det = PatternDetector(CONFIG)
    for ms, variant in ((0.25, "minstop=0.25"), (0.0, "minstop=0")):
        trades = []
        for sym, b in qualified.items():
            trades += walk_trades(det, b, ms)
        append_ledger(day_s, variant, trades)
        tot = sum(float(t["R"]) for t in trades)
        wins = sum(1 for t in trades if float(t["R"]) > 0)
        print(f"  [{variant:<13}] {len(trades)} trade(s), {wins} win(s), {tot:+.2f}R")
    print(f"  📒 universe -> {UNIVERSE_LOG} · trades -> {LEDGER}")


if __name__ == "__main__":
    main()
