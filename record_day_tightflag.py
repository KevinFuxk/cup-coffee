"""
record_day_tightflag.py — one archived watchlist day -> the HTF ledger
=======================================================================
The high-tight-flag twin of record_day.py (pivot 2026-09-02). For a day whose
source-tagged watchlist sits in data/watchlists/<day>.txt it:

  1. parses the ###SOURCE sections (record_day.parse_sources — import only)
  2. JOINS data/universe_log.csv READ-ONLY for the qualified/dropped verdicts.
     The cup channel's record_day.py OWNS that file — this script NEVER writes
     it. A symbol missing from the log gets its verdict computed locally with
     the same rules (tradable at IBKR + open >= $15) and is marked "unlogged"
     on stdout; nothing is silently dropped.
  3. pulls the day's 1-MINUTE bars per ticker — cache/ibkr1min_days/ first,
     IBKR otherwise (then cached; a "2 D" request also banks the PREVIOUS
     session, which supplies prev_close for the long gate)
  4. runs the frozen HTF rules (pattern_detector_tightflag.scan_day: 2:1
     tightness, side by bar-1 colour, stop-entry at the bars-1/2 extreme
     valid 09:32-09:45, R = entry-stop, 1.75R fly, 2-bar-lag trail, 15:49
     flat) on the QUALIFIED names and appends ledger rows to
     data/replay_trades_tightflag.csv — the cup ledger's exact 14-column
     schema, tf="1min", variant="htf", idempotent per (session, variant).

Schema notes for the shared columns: `trigger` = the resting stop-entry level
(the bars-1/2 extreme); `entry` = the fill (== trigger unless bar 3 gapped
through the order); `stop_pct` = |trigger-stop|/trigger*100 (descriptive, the
cup convention); `R` and `peak_R` use the FROZEN HTF risk unit |entry-stop|.
exit_kind: STOP (incl. trailing stop-outs) | EOD.

    python record_day_tightflag.py 2026-08-28
    python record_day_tightflag.py 2026-08-31
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date as Date, datetime, time as dtime
from zoneinfo import ZoneInfo

from data_layer import Bars
from pattern_detector_tightflag import CONFIG, scan_day

# USER 2026-09-03 universe policy: the index ETFs SPY and QQQ ARE tradable for this
# strategy; every OTHER ETF/ETN/fund stays excluded. The cup screen rejects all
# non-common stock, so those two (and only those two) get waved through here.
ETF_ALLOWED = {"SPY", "QQQ"}
from record_day import parse_sources                 # cup file — import only, never edited

ET = ZoneInfo("America/New_York")
UNIVERSE_LOG = "data/universe_log.csv"               # cup channel's file — READ ONLY here
LEDGER = "data/replay_trades_tightflag.csv"
BAR_CACHE = "cache/ibkr1min_days"
MIN_PRICE = 15.0                                     # same universe floor as the cup screen
VARIANT = "htf"
CLIENT_ID = 19                                       # daily-program assignment (HTF record)


def load_cached(sym: str, day: Date) -> list | None:
    p = f"{BAR_CACHE}/{sym}/{day}.json"
    return json.load(open(p)) if os.path.exists(p) else None


def save_cache(sym: str, day_s: str, rows: list) -> None:
    os.makedirs(f"{BAR_CACHE}/{sym}", exist_ok=True)
    json.dump(rows, open(f"{BAR_CACHE}/{sym}/{day_s}.json", "w"))


def rows_to_bars(sym: str, day: Date, rows: list) -> Bars | None:
    b = Bars(symbol=sym, date=day, timeframe="1min", ts=[], o=[], h=[], l=[], c=[], v=[])
    for r in rows:
        t = datetime.fromtimestamp(r["t"] / 1000, tz=ET).replace(tzinfo=None)
        if dtime(9, 30) <= t.time() <= dtime(15, 59):
            b.ts.append(t); b.o.append(r["o"]); b.h.append(r["h"])
            b.l.append(r["l"]); b.c.append(r["c"]); b.v.append(r["v"])
    return b if len(b) else None


def prev_close_from_cache(sym: str, day: Date) -> float | None:
    """Last RTH close of the latest CACHED session strictly before `day`."""
    d = f"{BAR_CACHE}/{sym}"
    if not os.path.isdir(d):
        return None
    older = sorted(f[:-5] for f in os.listdir(d)
                   if f.endswith(".json") and f[:-5] < day.isoformat())
    if not older:
        return None
    b = rows_to_bars(sym, Date.fromisoformat(older[-1]),
                     json.load(open(f"{d}/{older[-1]}.json")))
    return b.c[-1] if b else None


def fetch_two_days(ib, Stock, sym: str, day: Date) -> tuple[list | None, str | None]:
    """IBKR: the session + its predecessor in one request. Caches every complete
    day it sees; returns (target day's rows, error)."""
    c = Stock(sym, "SMART", "USD")
    try:
        ib.qualifyContracts(c)
        if not getattr(c, "conId", 0):
            return None, "not a tradable US stock at IBKR"
        end = f"{day:%Y%m%d} 23:59:59 US/Eastern"
        bl = ib.reqHistoricalData(c, endDateTime=end, durationStr="2 D",
                                  barSizeSetting="1 min", whatToShow="TRADES",
                                  useRTH=True, formatDate=2, keepUpToDate=False)
        ib.sleep(1.5)                                  # IBKR pacing
    except Exception as e:
        return None, str(e)
    if not bl:
        return None, "no bars for that session (halted / not yet listed / wrong day)"
    bydays: dict[str, list] = {}
    for x in bl:
        d = x.date.astimezone(ET).date().isoformat()
        bydays.setdefault(d, []).append(
            {"t": int(x.date.timestamp() * 1000), "o": float(x.open), "h": float(x.high),
             "l": float(x.low), "c": float(x.close), "v": float(x.volume)})
    for d, rows in bydays.items():
        if not os.path.exists(f"{BAR_CACHE}/{sym}/{d}.json"):
            save_cache(sym, d, rows)
    return bydays.get(day.isoformat()), None


def prev_close_ibkr(ib, Stock, sym: str, day: Date) -> float | None:
    """Fallback when no earlier session is cached (e.g. the first backfill day):
    the official daily close of the last session strictly before `day`."""
    try:
        c = Stock(sym, "SMART", "USD")
        ib.qualifyContracts(c)
        if not getattr(c, "conId", 0):
            return None
        bl = ib.reqHistoricalData(c, endDateTime=f"{day:%Y%m%d} 23:59:59 US/Eastern",
                                  durationStr="5 D", barSizeSetting="1 day",
                                  whatToShow="TRADES", useRTH=True, formatDate=2)
        ib.sleep(1)
        closes = [(x.date if isinstance(x.date, Date) else x.date.date(), float(x.close))
                  for x in bl]
        older = [c2 for d2, c2 in closes if d2 < day]
        return older[-1] if older else None
    except Exception:
        return None


def universe_verdicts(day_s: str) -> dict[str, dict]:
    """READ-ONLY join of the cup channel's universe log for this day."""
    if not os.path.exists(UNIVERSE_LOG):
        return {}
    return {r["symbol"]: r for r in csv.DictReader(open(UNIVERSE_LOG))
            if r.get("day") == day_s}


def event_to_row(e) -> dict:
    lng = e.side == "long"
    trigger = max(e.b1_h, e.b2_h) if lng else min(e.b1_l, e.b2_l)
    return {
        "symbol": e.symbol, "tf": "1min",
        "entry_time": e.entry_time, "entry": f"{e.entry_price:.2f}",
        "trigger": f"{trigger:.2f}", "stop": f"{e.stop_price:.2f}",
        "stop_pct": f"{abs(trigger - e.stop_price) / trigger * 100:.2f}",
        "exit_time": e.exit_time, "exit_kind": e.exit_reason.upper(),
        "exit": f"{e.exit_price:.2f}", "R": f"{e.pnl_R:.2f}",
        "peak_R": f"{e.mfe_R:.2f}",
    }


def append_ledger(day_s: str, trades: list[dict]) -> None:
    """Idempotent per (session, variant) — same convention as the cup ledger."""
    cols = ["session", "variant", "symbol", "tf", "entry_time", "entry", "trigger", "stop",
            "stop_pct", "exit_time", "exit_kind", "exit", "R", "peak_R"]
    old = []
    if os.path.exists(LEDGER):
        old = [r for r in csv.DictReader(open(LEDGER))
               if not (r.get("session") == day_s and r.get("variant") == VARIANT)]
    for t in trades:
        old.append({"session": day_s, "variant": VARIANT, **t})
    with open(LEDGER, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", restval="")
        w.writeheader()
        for r in sorted(old, key=lambda x: (x["session"], x["variant"], x["exit_time"])):
            w.writerow(r)


def record(day_s: str, ib=None, Stock=None) -> list[dict]:
    """The day's full pipeline. Pass a connected ib to reuse a session (the
    evening orchestrator does); otherwise connects itself on clientId 19."""
    day = Date.fromisoformat(day_s)
    wl = f"data/watchlists/{day_s}.txt"
    if not os.path.exists(wl):
        raise SystemExit(f"✗ {wl} not found — archive the day's source-tagged list first")
    tags = parse_sources(wl)
    verdicts = universe_verdicts(day_s)
    print(f"{day_s}: {len(tags)} tickers from {wl}  "
          f"(universe log: {len(verdicts)} verdict(s) for this day — read-only)")

    own_ib = ib is None
    if own_ib:
        from ib_async import IB, Stock as _Stock
        Stock = _Stock
        ib = IB()
        try:
            ib.connect("127.0.0.1", 4002, clientId=CLIENT_ID, timeout=10)
        except Exception as e:
            raise SystemExit(f"✗ can't reach IB Gateway on 4002  ({e})")
        ib.reqMarketDataType(1)

    qualified: dict[str, Bars] = {}
    for sym, srcs in tags.items():
        rows = load_cached(sym, day)
        err = None
        if rows is None:
            rows, err = fetch_two_days(ib, Stock, sym, day)
        b = rows_to_bars(sym, day, rows) if rows else None
        v = verdicts.get(sym)
        if v is not None:                              # the cup channel's verdict rules
            q, reason, logged = v["qualified"] == "yes", v.get("reason", ""), ""
            if not q and sym in ETF_ALLOWED and "not common stock" in reason:
                q, reason = True, ""
                logged = "  (HTF policy: SPY/QQQ allowed despite the cup ETF screen)"
        else:                                          # same rules, computed locally
            logged = "  (unlogged — verdict computed locally, NOT written to the log)"
            if b is None:
                q, reason = False, err or "no bars for that session"
            elif b.o[0] < MIN_PRICE:
                q, reason = False, f"open ${b.o[0]:.2f} < ${MIN_PRICE:.0f} floor"
            else:
                q, reason = True, ""
        if q and b is None:                            # log says yes but bars are missing
            q, reason = False, f"log says qualified but bars unavailable ({err})"
        mark = "✅" if q else "🚫"
        print(f"  {mark} {sym:<6} [{'+'.join(srcs)}]"
              + (f"  open {b.o[0]:.2f}" if b else "")
              + (f"  — {reason}" if reason else "") + logged)
        if q:
            qualified[sym] = b

    trades = []
    for sym, b in qualified.items():
        pc = prev_close_from_cache(sym, day)
        if pc is None:
            pc = prev_close_ibkr(ib, Stock, sym, day)
            if pc is not None:
                print(f"  · {sym}: prev close ${pc:.2f} fetched from IBKR daily bars "
                      f"(no earlier cached session)")
        if pc is None:
            print(f"  ⚠️ {sym}: no previous close available — the prev-close long gate "
                  f"is OFF for this symbol today")
        ev, why = scan_day(b, sym, day, CONFIG, prev_close=pc)
        if ev is None:
            print(f"  · {sym} no trade — {why}")
            continue
        row = event_to_row(ev)
        fly = f"  FLY x{ev.trail_moves}" if ev.fly else ""
        print(f"  ▶ {sym} {ev.side.upper()} {row['entry_time']} @ {row['entry']} "
              f"stop {row['stop']} -> {row['exit_kind']} {row['exit_time']} "
              f"@ {row['exit']}  {ev.pnl_R:+.2f}R (peak {ev.mfe_R:+.2f}R){fly}")
        trades.append(row)

    if own_ib:
        ib.disconnect()
    append_ledger(day_s, trades)
    tot = sum(float(t["R"]) for t in trades)
    wins = sum(1 for t in trades if float(t["R"]) > 0)
    print(f"  [{VARIANT}] {len(trades)} trade(s), {wins} win(s), {tot:+.2f}R  -> {LEDGER}")
    return trades


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python record_day_tightflag.py YYYY-MM-DD")
    record(sys.argv[1])
