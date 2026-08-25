"""
ibkr_history_tightflag.py — 15-20yr SPY/QQQ 5-min history via IBKR + tight-flag scan
====================================================================================
TIGHT-FLAG strategy namespace ONLY (does not touch live_trader_ibkr.py or any
cup-and-handle file). Two subcommands:

  pull   — connect to your running TWS / IB Gateway (paper login is fine) and
           walk SPY + QQQ 5-minute RTH bars BACKWARD from today, one week per
           request, until IBKR has nothing older (~15-20 years for these ETFs).
           * pacing-safe: 1 request / 11 s (IBKR caps ~60 per 10 min)
           * resumable: data/ibkr5_<sym>.checkpoint remembers the oldest week
             fetched; re-running continues from there
           * storage: cache/ibkr5/<SYM>/<YYYY-MM-DD>.json rows
             [{"t": epoch-ms UTC, "o","h","l","c","v"}, ...] — same row shape
             as the Polygon minute cache, but 5-MINUTE bars from IBKR TRADES.
           Full 20yr x 2 symbols ≈ 6-7 h wall clock — run it overnight:
               python3 ibkr_history_tightflag.py pull
           TWS/Gateway must be running with API enabled (default port 7497
           paper TWS; 4002 paper Gateway — both are tried). Free delayed data
           mode is requested automatically; no paid live subscription needed
           for historical index bars.

  scan   — offline; run the tight-flag detector + fly-trigger labeler over the
           pulled history and write the strategy's long-history pile:
               data/events_tightflag_ibkr.jsonl
           Kept SEPARATE from the Polygon-cache pile (different vendor, index-
           only universe) so the two are never silently mixed.

Index-only assumptions (valid for SPY/QQQ, asserted per day, NOT valid for
thin names): every 5-min window trades, so the two setup windows get cov=5
(IBKR native 5-min bars carry no 1-min coverage detail), and the entry price
is the OPEN of the 09:40-09:45 bar (= the first trade at/after 09:40 on an
instrument this liquid; the day is skipped if that bar is missing).
"""
from __future__ import annotations

import argparse, json, os, sys
from collections import Counter
from datetime import date as Date, datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")

CACHE = "cache/ibkr5"
OUT = "data/events_tightflag_ibkr.jsonl"
PACING_S = 11.0                 # 60 req / 10 min cap -> 1 per 11 s is safe
EMPTY_STOP = 6                  # consecutive empty weeks = start of history


# ----------------------------------------------------------------------------
# pull
# ----------------------------------------------------------------------------

def _day_path(sym: str, day: str) -> str:
    d = os.path.join(CACHE, sym)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, day + ".json")


def _store(sym: str, bars) -> int:
    """Merge a batch of ib_async bars into per-day JSON files (dedup by t)."""
    bydays: dict[str, dict[int, dict]] = {}
    for b in bars:
        dt = b.date if b.date.tzinfo else b.date.replace(tzinfo=timezone.utc)
        et = dt.astimezone(ET)
        t = int(dt.timestamp() * 1000)
        bydays.setdefault(et.date().isoformat(), {})[t] = {
            "t": t, "o": b.open, "h": b.high, "l": b.low,
            "c": b.close, "v": float(b.volume)}
    n = 0
    for day, rows in bydays.items():
        p = _day_path(sym, day)
        if os.path.exists(p):
            for r in json.load(open(p)):
                rows.setdefault(r["t"], r)
        out = [rows[t] for t in sorted(rows)]
        tmp = p + ".tmp"
        json.dump(out, open(tmp, "w"))
        os.replace(tmp, p)
        n += len(out)
    return n


def _connect(args):
    """Try both ports once. Returns a connected IB or None."""
    from ib_async import IB
    ib = IB()
    last_err = None
    for port in (args.port, args.alt_port):
        try:
            ib.connect(args.host, port, clientId=args.client_id, timeout=8)
            print(f"connected to IBKR on {args.host}:{port}")
            ib.reqMarketDataType(3)     # free delayed mode suffices for historical
            return ib
        except Exception as ex:
            last_err = ex
    print(f"  (no TWS/Gateway on ports {args.port}/{args.alt_port}: {last_err})")
    return None


def _reconnect(args, max_wait_s: int = 1800):
    """Keep trying every 30 s for up to 30 min (rides out TWS's nightly
    auto-restart). Returns a connected IB or None."""
    import time
    waited = 0
    while waited <= max_wait_s:
        ib = _connect(args)
        if ib is not None:
            return ib
        time.sleep(30)
        waited += 30
    return None


def fill(args):
    """Pull a SPECIFIC window backward from --end for --weeks weeks, without
    touching the main checkpoint. Used to patch verified holes (e.g. the QQQ
    2022-03-10..2022-04-13 gap) and to probe an older listing venue via
    --exchange (SPY traded on AMEX before its NYSE Arca listing)."""
    import time
    from datetime import timedelta
    from ib_async import Stock
    ib = _connect(args)
    if ib is None:
        print("ERROR: cannot reach TWS/IB Gateway — start it and re-run.")
        sys.exit(1)
    sym = args.symbols.split(",")[0].strip().upper()
    exch = args.exchange or {"SPY": "ARCA", "QQQ": "NASDAQ"}.get(sym, "SMART")
    contract = Stock(sym, exch, "USD")
    if args.primary:
        contract.primaryExchange = args.primary
    ib.qualifyContracts(contract)
    print(f"fill {sym}@{exch}"
          f"{'/' + args.primary if args.primary else ''}: "
          f"{args.weeks} week(s) back from {args.end}")
    end = datetime.fromisoformat(args.end + " 23:59:59").replace(tzinfo=timezone.utc)
    got = 0
    for w in range(args.weeks):
        try:
            bars = ib.reqHistoricalData(
                contract, endDateTime=end, durationStr="1 W",
                barSizeSetting="5 mins", whatToShow="TRADES",
                useRTH=True, formatDate=2)
        except Exception as ex:
            print(f"  week {w+1}: request failed ({ex})")
            break
        if not bars:
            print(f"  week {w+1} ending {end.date()}: NO DATA")
        else:
            got += _store(sym, bars)
            earliest = bars[0].date
            if not earliest.tzinfo:
                earliest = earliest.replace(tzinfo=timezone.utc)
            print(f"  week {w+1}: back to {earliest.astimezone(ET).date()} (+{len(bars)} bars)")
            end = earliest
            time.sleep(PACING_S)
            continue
        end = end - timedelta(days=7)
        time.sleep(PACING_S)
    ib.disconnect()
    print(f"fill done: {got} rows written for {sym}")


def pull(args):
    import time
    from datetime import timedelta
    from ib_async import Stock
    ib = _connect(args)
    if ib is None:
        print("ERROR: cannot reach TWS/IB Gateway.\n"
              "Start TWS or IB Gateway (paper login is fine, API enabled), then re-run:\n"
              "    python3 ibkr_history_tightflag.py pull")
        sys.exit(1)

    start_floor = Date.fromisoformat(args.start)
    exch = {"SPY": "ARCA", "QQQ": "NASDAQ"}
    for sym in args.symbols.split(","):
        sym = sym.strip().upper()
        contract = Stock(sym, exch.get(sym, "SMART"), "USD")
        ib.qualifyContracts(contract)
        ckpt = f"data/ibkr5_{sym}.checkpoint"
        end = ""                                    # "" = now
        if os.path.exists(ckpt):
            end = datetime.fromisoformat(open(ckpt).read().strip())
            print(f"{sym}: resuming before {end}")
        empties = 0
        fails = 0
        total = 0
        while True:
            # one iteration = one 1-week request. ANY dropped socket (Mac
            # slept, TWS nightly restart, network blip) -> reconnect & retry;
            # the checkpoint written after every batch makes this loss-free.
            try:
                bars = ib.reqHistoricalData(
                    contract, endDateTime=end, durationStr="1 W",
                    barSizeSetting="5 mins", whatToShow="TRADES",
                    useRTH=True, formatDate=2)
                fails = 0
            except Exception as ex:
                if not ib.isConnected():
                    print(f"{sym}: connection lost ({type(ex).__name__}) — "
                          f"reconnecting (progress is checkpointed)...")
                    ib = _reconnect(args)
                    if ib is None:
                        print("could not reconnect within 30 min — progress is saved; "
                              "re-run the same command to resume")
                        sys.exit(1)
                    ib.qualifyContracts(contract)
                    continue
                fails += 1
                if fails >= 3:
                    print(f"{sym}: 3 straight request failures at end={end} ({ex}) "
                          f"— stopping this symbol")
                    break
                print(f"{sym}: request failed ({ex}); retrying in 30s...")
                time.sleep(30)
                continue
            if not bars:
                empties += 1
                if empties >= EMPTY_STOP:
                    print(f"{sym}: {EMPTY_STOP} empty weeks — start of history reached")
                    break
                # step the window back a week manually and keep probing
                ref = end if isinstance(end, datetime) else datetime.now(timezone.utc)
                end = ref - timedelta(days=7)
                open(ckpt, "w").write(end.isoformat())
                time.sleep(PACING_S)
                continue
            empties = 0
            total += _store(sym, bars)
            earliest = bars[0].date
            if not earliest.tzinfo:
                earliest = earliest.replace(tzinfo=timezone.utc)
            end = earliest
            open(ckpt, "w").write(end.isoformat())
            first_day = earliest.astimezone(ET).date()
            print(f"  {sym}: back to {first_day}  (+{len(bars)} bars, {total} rows this run)")
            if first_day <= start_floor:
                print(f"{sym}: reached start floor {start_floor}")
                break
            time.sleep(PACING_S)
    ib.disconnect()
    print("pull done.")


# ----------------------------------------------------------------------------
# scan (offline)
# ----------------------------------------------------------------------------

def scan(args):
    from data_layer import Bars
    from pattern_detector_tightflag import (CONFIG, detect, label_trail,
                                            TightFlagEvent, event_record,
                                            prev_close_gate, r_unit_for,
                                            entry_fill)
    _EOD = CONFIG["eod_flat"]
    funnel = Counter()
    per_year = Counter()
    per_sym_year = Counter()
    synth_days = []
    n_ev = 0
    want = {s.strip().upper() for s in args.symbols.split(",")} if args.symbols else None
    lo = args.start or "0000-00-00"
    hi = args.end or "9999-99-99"
    with open(OUT, "w") as fout:
        for sym in sorted(os.listdir(CACHE)):
            d = os.path.join(CACHE, sym)
            if not os.path.isdir(d) or (want and sym not in want):
                continue
            prev_close = None          # yesterday's 16:00 close, for the long gate
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".json") or not (lo <= fn[:-5] <= hi):
                    continue
                day = Date.fromisoformat(fn[:-5])
                rows = json.load(open(os.path.join(d, fn)))
                o=[];h=[];l=[];c=[];v=[];ts=[];buckets=[]
                for r in sorted(rows, key=lambda r: r["t"]):
                    dt = datetime.fromtimestamp(r["t"]/1000, tz=timezone.utc).astimezone(ET)
                    mod = dt.hour*60 + dt.minute
                    k = (mod - 570)//5
                    if mod < 570 or mod >= 960:
                        continue
                    if buckets and k == buckets[-1]:
                        # SAME clock window -> MERGE, exactly like clock_5min. (Dropping
                        # the row instead silently shrinks the window's range, which can
                        # flip both detection and the fly trigger.)
                        h[-1] = max(h[-1], r["h"]); l[-1] = min(l[-1], r["l"])
                        c[-1] = r["c"]; v[-1] += r["v"]
                        continue
                    if buckets and k < buckets[-1]:
                        continue                       # out-of-order stray row
                    o.append(r["o"]); h.append(r["h"]); l.append(r["l"])
                    c.append(r["c"]); v.append(r["v"]); ts.append(dt); buckets.append(k)
                if len(o) < 3:
                    funnel["too_few_bars"] += 1
                    continue                      # no usable close -> leave prev_close
                five = Bars(sym, day, "5min", ts, o, h, l, c, v, True)
                cov = [5]*len(o)     # index-only assumption: every window trades
                today_close = five.c[-1]        # last RTH bar's close = today's close
                setup, why = detect(five, buckets, cov, CONFIG)
                if setup is None:
                    funnel[why] += 1
                    prev_close = today_close
                    continue
                if prev_close_gate(setup, prev_close, CONFIG):
                    funnel["long_below_prev_close"] += 1
                    prev_close = today_close
                    continue
                lng = setup["side"] == "long"
                stop0 = setup["l2"] if lng else setup["h2"]
                # USER SPEC 2026-07-28: resting stop-entry at the extreme of bars 1-2,
                # triggerable during BAR 3 only (a touch is enough).
                fill = entry_fill(five, buckets, setup, CONFIG)
                if fill is None:
                    funnel["no_trigger"] += 1
                    prev_close = today_close
                    continue
                entry_price, entry_ts, delay = fill
                ent = next(i for i, k in enumerate(buckets) if k == CONFIG["trigger_bar"])
                r_unit = r_unit_for(entry_price, stop0, setup["range2"], CONFIG)
                if r_unit <= 0:
                    funnel["zero_r_unit"] += 1
                    prev_close = today_close
                    continue
                lab = label_trail(five, buckets, entry_price, stop0,
                                  r_unit, setup["side"], CONFIG)
                e = TightFlagEvent(
                    symbol=sym, day=day, timeframe="5min", side=setup["side"],
                    b1_o=five.o[0], b1_h=five.h[0], b1_l=five.l[0], b1_c=five.c[0], b1_v=five.v[0],
                    b2_o=five.o[1], b2_h=five.h[1], b2_l=five.l[1], b2_c=five.c[1], b2_v=five.v[1],
                    cov1=5, cov2=5,
                    range1=setup["range1"], range2=setup["range2"], ratio=setup["ratio"],
                    high_diff=setup["high_diff"], high_diff_frac=setup["high_diff_frac"],
                    low_diff=setup["low_diff"], low_diff_frac=setup["low_diff_frac"],
                    bar1_green=five.c[0] > five.o[0], bar2_green=five.c[1] > five.o[1],
                    inside_bar=(five.h[1] <= five.h[0] and five.l[1] >= five.l[0]),
                    entry_time=entry_ts.strftime("%H:%M"), entry_delay_min=delay,
                    entry_price=entry_price, stop_price=stop0, r_unit=r_unit,
                    entry_stop_R=abs(entry_price - stop0)/r_unit,
                    **lab)
                rec = event_record(e)
                # research flag: IBKR emits o==h==l==c zero-volume carry-forward
                # bars when nothing traded. Harmless for the setup (a zero-range
                # bar2 is already rejected) but an EOD flat priced off a run of
                # them would be fiction — count them in the held window.
                nsyn = sum(1 for i in range(ent, len(five))
                           if five.v[i] == 0 and five.h[i] == five.l[i])
                rec["synthetic_bars_in_hold"] = nsyn
                rec["prev_close"] = prev_close
                rec["b1h_vs_prev_close"] = (five.h[0] - prev_close) if prev_close else None
                if nsyn:
                    synth_days.append((sym, fn[:-5], nsyn, e.exit_reason))
                fout.write(json.dumps(rec) + "\n")
                funnel["DETECTED"] += 1
                per_year[fn[:4]] += 1
                per_sym_year[(sym, fn[:4])] += 1
                n_ev += 1
                prev_close = today_close
    print(f"wrote {OUT}: {n_ev} events")
    print("funnel:", dict(funnel.most_common()))
    print("per year:", dict(sorted(per_year.items())))
    syms = sorted({s for s, _ in per_sym_year})
    print("per symbol x year (blank = that symbol has NO history that year — an\n"
          "  unbalanced era reweights every aggregate, so check this before pooling):")
    for y in sorted({y for _, y in per_sym_year}):
        print("   ", y, "  ".join(f"{s}={per_sym_year.get((s, y), 0):<4}" for s in syms))
    if synth_days:
        eod = [x for x in synth_days if x[3] == "eod"]
        print(f"zero-volume synthetic bars inside the held window on {len(synth_days)} events "
              f"({len(eod)} of them EOD floats — those exit prices are the fabricated ones):")
        for x in sorted(synth_days, key=lambda x: -x[2])[:8]:
            print(f"    {x[0]} {x[1]}  {x[2]} synthetic bars  exit={x[3]}")
    months = Counter()
    for line in open(OUT):
        months[json.loads(line)["day"][:7]] += 1
    yrs = sorted({m[:4] for m in months})
    print(f"months with data: {len(months)} across {yrs[0] if yrs else '-'}..{yrs[-1] if yrs else '-'}"
          f"  (check for gap months before trusting totals)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("cmd", choices=["pull", "scan", "probe", "fill"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497)       # TWS paper
    ap.add_argument("--alt-port", type=int, default=4002)   # Gateway paper
    ap.add_argument("--client-id", type=int, default=77)
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--start", default="2004-01-01")        # pull floor
    ap.add_argument("--end", default="")                    # fill: window end (YYYY-MM-DD)
    ap.add_argument("--weeks", type=int, default=6)         # fill: how many weeks back
    ap.add_argument("--exchange", default="")               # fill: override venue
    ap.add_argument("--primary", default="")                # fill: primaryExchange
    args = ap.parse_args()
    if args.cmd == "probe":
        from ib_async import IB
        ib = IB()
        for port in (args.port, args.alt_port):
            try:
                ib.connect(args.host, port, clientId=args.client_id, timeout=6)
                print(f"OK: TWS/Gateway reachable on port {port}")
                ib.disconnect()
                sys.exit(0)
            except Exception as ex:
                print(f"port {port}: {ex}")
        sys.exit(1)
    elif args.cmd == "pull":
        pull(args)
    elif args.cmd == "fill":
        fill(args)
    else:
        scan(args)
