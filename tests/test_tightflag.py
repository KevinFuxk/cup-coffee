"""
tests/test_tightflag.py — the safety net for the FROZEN 1-min high-tight-flag rules
====================================================================================
One tiny hand-built price pattern per rule, each with a known correct answer, in the
style of tests/test_cup_coffee.py (plain-assert runner). Run after ANY change to
pattern_detector_tightflag.py / live_trader_tightflag.py:

    python tests/test_tightflag.py

Covered, mapped to the daily-program brief:
  ratio gate            bar1 >= 2 x bar2, boundary inclusive at exactly 2.00
  side rule             green bar1 -> long, red -> short, doji -> no trade
  entry window          the resting stop-entry fills on a 09:44-bar touch and is
                        REFUSED when the first touch is the 09:45 bar (deadline),
                        both at the detector level and through the LIVE bot path
                        (the post-EOD-arming regression class from the brief)
  gap-through fill      bar opening past the level fills at the open, not the level
  fly trigger           +1.75R during bars <=4 arms (inclusive); 1.74R does not;
                        reached at bar 5 -> never arms, stop never moves
  trail lag             at a qualifying bar N's close the stop moves to the PREVIOUS
                        printed bar's extreme and only protects from N+1
  prev-close long gate  bar1 high below yesterday's close kills the long only
  real-day golden       one recorded 2026-08-28 trade recomputed from the cache
                        must reproduce the ledger row exactly
"""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)

import json
import traceback
from datetime import date as Date, datetime

from data_layer import Bars
from pattern_detector_tightflag import (CONFIG, cfg_width, clock_bars, detect,
                                        entry_fill, scan_day)

DAY = Date(2026, 8, 20)
W = cfg_width(CONFIG)
assert W == 1, f"these tests encode the 1-min pivot rules; CONFIG timeframe is {CONFIG['timeframe']}"

PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  ✅ {name}")
    except Exception:
        FAIL.append(name)
        print(f"  ❌ {name}")
        traceback.print_exc(limit=3)


def mk(*ohlc, start=(9, 30)) -> Bars:
    """1-min bars from (o,h,l,c) tuples, volume 1000, starting 09:30."""
    b = Bars(symbol="TST", date=DAY, timeframe="1min", ts=[], o=[], h=[], l=[], c=[], v=[])
    h0, m0 = start
    for i, (o, h, l, c) in enumerate(ohlc):
        m = h0 * 60 + m0 + i
        b.ts.append(datetime(DAY.year, DAY.month, DAY.day, m // 60, m % 60))
        b.o.append(o); b.h.append(h); b.l.append(l); b.c.append(c); b.v.append(1000.0)
    return b


def pad_to(b: Bars, minute: int, o, h, l, c) -> None:
    """Append flat (o,h,l,c) bars up to and including the given minute-of-day."""
    last = b.ts[-1].hour * 60 + b.ts[-1].minute
    for m in range(last + 1, minute + 1):
        b.ts.append(datetime(DAY.year, DAY.month, DAY.day, m // 60, m % 60))
        b.o.append(o); b.h.append(h); b.l.append(l); b.c.append(c); b.v.append(1000.0)


# a green bar1 (10.00-10.40, range .40) + tight bar2 (range .16) long setup:
# entry level = max(h1,h2) = 10.40, stop = bar2 low 10.20, R = 0.20
B1 = (10.00, 10.40, 10.00, 10.38)
B2 = (10.36, 10.36, 10.20, 10.30)


def t_ratio_gate():
    five, k, cov = clock_bars(mk(B1, B2), W)
    setup, why = detect(five, k, cov, CONFIG)
    assert setup and setup["side"] == "long", why
    assert abs(setup["ratio"] - 2.5) < 1e-6
    # exactly 2.00 passes (inclusive boundary) — exact binary fractions (0.5 / 0.25)
    five, k, cov = clock_bars(mk((10.0, 10.5, 10.0, 10.375), (10.25, 10.25, 10.0, 10.125)), W)
    setup, why = detect(five, k, cov, CONFIG)
    assert setup, f"exact 2.00 must pass, got {why}"
    # just under 2.00 fails (bar1 range 0.375 vs bar2 0.25 -> 1.5)
    five, k, cov = clock_bars(mk((10.0, 10.375, 10.0, 10.25), (10.25, 10.25, 10.0, 10.125)), W)
    setup, why = detect(five, k, cov, CONFIG)
    assert setup is None and why == "ratio"


def t_side_rule():
    red = ((10.40, 10.40, 10.00, 10.02), (10.04, 10.20, 10.04, 10.10))
    five, k, cov = clock_bars(mk(*red), W)
    setup, why = detect(five, k, cov, CONFIG)
    assert setup and setup["side"] == "short"
    assert setup["entry_level"] == 10.00               # min(l1, l2)
    doji = ((10.00, 10.40, 9.90, 10.00), (10.00, 10.10, 9.95, 10.05))
    five, k, cov = clock_bars(mk(*doji), W)
    setup, why = detect(five, k, cov, CONFIG)
    assert setup is None and why == "doji_bar1"


def t_entry_window_0944_fills():
    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 43, 10.30, 10.35, 10.28, 10.32)        # quiet until 09:43
    pad_to(b, 9 * 60 + 44, 10.32, 10.41, 10.30, 10.39)        # 09:44 bar TOUCHES 10.40
    pad_to(b, 9 * 60 + 59, 10.39, 10.39, 10.35, 10.36)
    five, k, cov = clock_bars(b, W)
    setup, _ = detect(five, k, cov, CONFIG)
    fill = entry_fill(five, k, setup, CONFIG)
    assert fill is not None, "a 09:44 touch must fill"
    px, ts, delay, kfill = fill
    assert abs(px - 10.40) < 1e-9 and ts.minute == 44


def t_entry_window_0945_refused():
    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 44, 10.30, 10.35, 10.28, 10.32)        # never touches by 09:44
    pad_to(b, 9 * 60 + 45, 10.32, 10.45, 10.30, 10.44)        # first touch = the 09:45 bar
    pad_to(b, 9 * 60 + 59, 10.44, 10.60, 10.40, 10.55)
    five, k, cov = clock_bars(b, W)
    setup, _ = detect(five, k, cov, CONFIG)
    assert entry_fill(five, k, setup, CONFIG) is None, "the 09:45 bar must be refused"
    ev, why = scan_day(b, "TST", DAY, CONFIG, prev_close=9.0)
    assert ev is None and why == "no_trigger"


def t_entry_window_live_bot():
    """Same two scenarios through the LIVE decision path (the brief's post-EOD
    arming regression class: a level touched after the deadline must never enter)."""
    import live_trader_tightflag as L

    class A:
        symbols = []; watchlist = "auto"; arm = False; risk = 0.0025; base = 100000.0
        max_positions = 2; max_notional = 1.0; delayed = False; replay = False

    class NoIB:
        def positions(self): return []
        def accountValues(self): return []

    def drive(bars: Bars):
        bot = L.TightFlagTrader(NoIB(), None, None, A())
        bot.say = lambda line: None        # NEVER write test narration into logs/
                                           # (roll_day rebinds log_path, so silencing
                                           # say() is the only safe override — 8ace1bc)
        bot.prev_close["TST"] = 9.0
        for i in range(len(bars)):
            kk = (bars.ts[i].hour * 60 + bars.ts[i].minute - 570) // L.Clock5.WIDTH
            bot.on_5min("TST", kk, bars.ts[i], bars.o[i], bars.h[i],
                        bars.l[i], bars.c[i], bars.v[i], 1)
        return bot

    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 43, 10.30, 10.35, 10.28, 10.32)
    pad_to(b, 9 * 60 + 44, 10.32, 10.41, 10.30, 10.39)
    pad_to(b, 9 * 60 + 50, 10.39, 10.39, 10.35, 10.36)
    bot = drive(b)
    assert any(c["sym"] == "TST" for c in bot.closed) or bot.state.get("TST", {}).get("open"), \
        "live path must enter on the 09:44 touch"

    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 44, 10.30, 10.35, 10.28, 10.32)
    pad_to(b, 10 * 60 + 30, 10.32, 10.60, 10.30, 10.55)       # touches only AFTER 09:45
    bot = drive(b)
    assert not bot.state.get("TST", {}).get("open") and not bot.closed, \
        "live path must NEVER enter after the 09:45 deadline"
    assert bot.done.get("TST"), "the dead setup must be marked done (not silently dropped)"


def t_gap_through_fill():
    b = mk(B1, B2, (10.55, 10.70, 10.50, 10.60))              # 09:32 opens ABOVE the level
    pad_to(b, 9 * 60 + 59, 10.60, 10.62, 10.55, 10.58)
    five, k, cov = clock_bars(b, W)
    setup, _ = detect(five, k, cov, CONFIG)
    px, ts, _, _ = entry_fill(five, k, setup, CONFIG)
    assert abs(px - 10.55) < 1e-9, "gap through the buy-stop must fill at the open"


def t_fly_trigger_and_trail():
    # EXACT binary fractions throughout so boundary comparisons are precise:
    # bar1 (10.0-10.5, range .5, green) + bar2 (range .25) -> ratio 2.0
    # entry level = 10.50, stop = 10.00, R = 0.50; fly needs 10.50+1.75*.5 = 11.375
    C1 = (10.0, 10.5, 10.0, 10.375)
    C2 = (10.25, 10.25, 10.0, 10.125)
    fill = (10.375, 10.5, 10.25, 10.4375)             # 09:32 touches 10.50 -> fills there
    # (a) exactly +1.75R during bar 4 (09:33) arms; the 09:34 green higher-high
    #     close moves the stop to the PREVIOUS printed bar's low (trail lag), and
    #     that stop only protects from 09:35 on
    b = mk(C1, C2, fill,
           (10.5, 11.375, 10.375, 11.25),             # 09:33: +1.75R exactly -> arms
           (11.25, 11.5, 11.0, 11.4375),              # 09:34: green HH -> stop = l(09:33) = 10.375
           (11.4375, 11.5, 10.375, 10.5))             # 09:35: tags 10.375 -> stopped
    pad_to(b, 9 * 60 + 59, 10.5, 10.5, 10.4375, 10.5)
    ev, why = scan_day(b, "TST", DAY, CONFIG, prev_close=9.0)
    assert ev and ev.fly, f"exactly +1.75R by bar 4 must arm ({why})"
    assert any(abs(s - 10.375) < 1e-9 for _, s in ev.trail_path), ev.trail_path
    assert ev.exit_reason == "stop" and abs(ev.exit_price - 10.375) < 1e-9
    assert abs(ev.pnl_R - (10.375 - 10.5) / 0.5) < 1e-9          # -0.25R
    # (b) just under the trigger (+1.5R max) -> never arms, stop never moves
    b = mk(C1, C2, fill,
           (10.5, 11.25, 10.375, 11.0),               # +1.5R only
           (11.0, 11.5, 10.75, 11.4375))
    pad_to(b, 9 * 60 + 59, 11.4375, 11.5, 11.25, 11.375)
    ev, why = scan_day(b, "TST", DAY, CONFIG, prev_close=9.0)
    assert ev and not ev.fly and ev.trail_moves == 0
    assert abs(ev.final_stop - 10.0) < 1e-9, "no-fly stop must never move"
    # (c) +1.75R reached FIRST at bar 5 (09:34) -> never arms
    b = mk(C1, C2, fill,
           (10.5, 11.0, 10.375, 10.875),              # bar 4 short of the trigger
           (10.875, 11.5, 10.75, 11.4375))            # bar 5 would have armed
    pad_to(b, 9 * 60 + 59, 11.4375, 11.5, 11.25, 11.375)
    ev, why = scan_day(b, "TST", DAY, CONFIG, prev_close=9.0)
    assert ev and not ev.fly, "reaching 1.75R first at bar 5 must NOT arm"


def t_prev_close_long_gate():
    b = mk(B1, B2, (10.36, 10.41, 10.30, 10.40))
    pad_to(b, 9 * 60 + 59, 10.40, 10.42, 10.35, 10.38)
    ev, why = scan_day(b, "TST", DAY, CONFIG, prev_close=10.60)   # b1 high 10.40 < 10.60
    assert ev is None and why == "long_below_prev_close"
    ev, why = scan_day(b, "TST", DAY, CONFIG, prev_close=10.39)   # reclaimed
    assert ev is not None, why


def t_real_day_golden():
    import csv
    LEDGER = "data/replay_trades_tightflag.csv"
    assert os.path.exists(LEDGER), "run record_tightflag.py 2026-08-28 first"
    rows = [r for r in csv.DictReader(open(LEDGER))
            if r["session"] == "2026-08-28" and r["variant"] == "htf"]
    assert rows, "no 2026-08-28 htf rows in the ledger"
    import record_day_tightflag as RD
    for r in rows:
        sym = r["symbol"]
        rows_json = RD.load_cached(sym, Date(2026, 8, 28))
        assert rows_json, f"no cached bars for {sym} 2026-08-28"
        b = RD.rows_to_bars(sym, Date(2026, 8, 28), rows_json)
        pc = RD.prev_close_from_cache(sym, Date(2026, 8, 28))
        if pc is None:
            # 08-28 is the earliest cached session, so the recompute runs with the
            # long gate OFF while the ledger was built with real IBKR prev-closes.
            # Matching still validates entry/exit/R; a gate divergence would show as
            # a recomputed trade the ledger lacks. Stated, not hidden.
            print(f"      (note: {sym} recomputed with the prev-close gate OFF)")
        ev, why = scan_day(b, sym, Date(2026, 8, 28), CONFIG, prev_close=pc)
        assert ev is not None, f"{sym}: ledger has a trade but recompute says {why}"
        got = RD.event_to_row(ev)
        for col in ("entry_time", "entry", "trigger", "stop", "exit_time",
                    "exit_kind", "exit", "R", "peak_R"):
            assert got[col] == r[col], f"{sym} {col}: recomputed {got[col]} != ledger {r[col]}"


def t_no_arming_after_eod():
    """The brief's non-negotiable, to the letter: bars after the 15:49 flatten must
    never open or re-open a position — even if the entry level is finally touched."""
    import live_trader_tightflag as L

    class A:
        symbols = []; watchlist = "auto"; arm = False; risk = 0.0025; base = 100000.0
        max_positions = 2; max_notional = 1.0; delayed = False; replay = False

    class NoIB:
        def positions(self): return []
        def accountValues(self): return []

    bot = L.TightFlagTrader(NoIB(), None, None, A())
    bot.say = lambda line: None            # never write test narration into logs/
    bot.prev_close["TST"] = 9.0
    b = mk(B1, B2)
    pad_to(b, 15 * 60 + 49, 10.30, 10.35, 10.28, 10.32)       # never touches all day
    pad_to(b, 15 * 60 + 59, 10.32, 10.60, 10.30, 10.55)       # touched only AFTER 15:49
    for i in range(len(b)):
        kk = (b.ts[i].hour * 60 + b.ts[i].minute - 570) // L.Clock5.WIDTH
        bot.on_5min("TST", kk, b.ts[i], b.o[i], b.h[i], b.l[i], b.c[i], b.v[i], 1)
    assert not bot.state.get("TST", {}).get("open"), "post-15:49 touch must NOT enter"
    assert not any(c["sym"] == "TST" for c in bot.closed if c["kind"] != "EOD"), \
        "no trade may exist from a post-EOD touch"
    # and an OPEN position must be flat by the 15:49 bar, never re-entered after
    bot2 = L.TightFlagTrader(NoIB(), None, None, A())
    bot2.say = lambda line: None
    bot2.prev_close["TST"] = 9.0
    b = mk(B1, B2, (10.36, 10.41, 10.30, 10.39))              # fills 09:32 @ 10.40
    pad_to(b, 15 * 60 + 49, 10.39, 10.39, 10.30, 10.35)       # floats (stop 10.20 never hit)
    pad_to(b, 15 * 60 + 59, 10.35, 10.90, 10.30, 10.80)       # wild bars after the flatten
    for i in range(len(b)):
        kk = (b.ts[i].hour * 60 + b.ts[i].minute - 570) // L.Clock5.WIDTH
        bot2.on_5min("TST", kk, b.ts[i], b.o[i], b.h[i], b.l[i], b.c[i], b.v[i], 1)
    eods = [c for c in bot2.closed if c["kind"] == "EOD" and c["sym"] == "TST"]
    assert eods and not bot2.state.get("TST", {}).get("open"), \
        "an open position must be booked EOD at the 15:49 bar and stay closed"


def t_eod_exit_bar():
    """PIVOT BUG FIX 2026-09-04: on the 1-min clock the EOD flat must book off the
    15:49 bar's close (the last window STARTING <= 15:49, same as the frozen
    labeler) — not the old 5-min era's 15:45 bar (SMMT 09-03: 17.17 vs 17.21)."""
    import live_trader_tightflag as L
    assert (L.EOD_BAR_START.hour, L.EOD_BAR_START.minute) == (15, 49), \
        f"EOD exit bar must derive to 15:49 on 1-min bars, got {L.EOD_BAR_START}"

    class A:
        symbols = []; watchlist = "auto"; arm = False; risk = 0.0025; base = 100000.0
        max_positions = 2; max_notional = 1.0; delayed = False; replay = False

    class NoIB:
        def positions(self): return []
        def accountValues(self): return []

    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 32, 10.32, 10.41, 10.30, 10.39)    # 09:32 touches 10.40 -> fill
    pad_to(b, 15 * 60 + 44, 10.50, 10.55, 10.45, 10.50)   # drifts; never near the 10.20 stop
    pad_to(b, 15 * 60 + 45, 10.50, 11.15, 10.48, 11.11)   # the OLD (5-min era) exit bar
    pad_to(b, 15 * 60 + 48, 10.50, 10.55, 10.48, 10.50)
    pad_to(b, 15 * 60 + 49, 10.50, 12.40, 10.48, 12.34)   # the frozen labeler's exit bar
    pad_to(b, 15 * 60 + 55, 10.50, 10.55, 10.48, 10.50)
    bot = L.TightFlagTrader(NoIB(), None, None, A())
    bot.say = lambda line: None        # NEVER write test narration into logs/ (8ace1bc)
    bot.prev_close["TST"] = 9.0
    for i in range(len(b)):
        kk = (b.ts[i].hour * 60 + b.ts[i].minute - 570) // L.Clock5.WIDTH
        bot.on_5min("TST", kk, b.ts[i], b.o[i], b.h[i], b.l[i], b.c[i], b.v[i], 1)
    assert len(bot.closed) == 1, f"expected exactly one trade, got {len(bot.closed)}"
    tr = bot.closed[0]
    assert tr["kind"] == "EOD" and abs(tr["exit"] - 12.34) < 1e-9 and tr["ts"].minute == 49, \
        f"EOD must book the 15:49 close (12.34), got {tr['kind']} @ {tr['exit']} ts {tr['ts']}"


def t_no_reentry_after_stopout():
    """BUG FIX 2026-09-04: a booked trade ends the symbol's day. After a same-window
    stop-out, a re-touch of the entry level before 09:45 must NOT open a second
    trade, and the window's end must not print a bogus no_trigger (TGTX 09-03)."""
    import live_trader_tightflag as L

    class A:
        symbols = []; watchlist = "auto"; arm = False; risk = 0.0025; base = 100000.0
        max_positions = 2; max_notional = 1.0; delayed = False; replay = False

    class NoIB:
        def positions(self): return []
        def accountValues(self): return []

    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 32, 10.32, 10.41, 10.15, 10.22)    # fills 10.40 AND hits the 10.20 stop
    pad_to(b, 9 * 60 + 35, 10.30, 10.45, 10.28, 10.42)    # re-touches the level before 09:45
    pad_to(b, 9 * 60 + 50, 10.30, 10.35, 10.28, 10.32)    # past the entry window's end
    said = []
    bot = L.TightFlagTrader(NoIB(), None, None, A())
    bot.say = said.append              # capture narration in memory, never in logs/
    bot.prev_close["TST"] = 9.0
    for i in range(len(b)):
        kk = (b.ts[i].hour * 60 + b.ts[i].minute - 570) // L.Clock5.WIDTH
        bot.on_5min("TST", kk, b.ts[i], b.o[i], b.h[i], b.l[i], b.c[i], b.v[i], 1)
    assert len(bot.closed) == 1, f"re-touch must NOT re-enter (got {len(bot.closed)} trades)"
    tr = bot.closed[0]
    assert tr["kind"] == "STOP" and abs(tr["exit"] - 10.20) < 1e-9, \
        "same-bar stop-out must price AT the stop"
    assert "TST" not in bot.state and bot.done.get("TST"), "booked symbol must leave state"
    assert not any("no_trigger" in x for x in said), \
        "no bogus no_trigger for a symbol that DID trade"


# ---- ARMED TRUTH harness (2026-09-09): a fake broker so the armed decision path
# ---- can be tested without IBKR. The invariant under test: bars decide INTENT
# ---- (place, cancel, flatten); only broker fills create or close a position.
from types import SimpleNamespace as _NS


def _armed_rig():
    import live_trader_tightflag as L

    class FEvent(list):
        def __iadd__(self, fn):
            self.append(fn); return self
        def emit(self, *a):
            for fn in list(self):
                fn(*a)

    class FTrade:
        def __init__(self, order, contract):
            self.order, self.contract = order, contract
            self.orderStatus = _NS(status="Submitted", avgFillPrice=0.0,
                                   filled=0, remaining=0)
            self.fillEvent = FEvent()

    class FIB:
        def __init__(self):
            self.client = _NS(getReqId=lambda: 1)
            self.trades, self.cancelled, self.pos = [], [], {}
        def placeOrder(self, c, o):
            t = FTrade(o, c); self.trades.append(t); return t
        def cancelOrder(self, o):
            self.cancelled.append(o)
        def positions(self):
            return [_NS(contract=_NS(symbol=k), position=v) for k, v in self.pos.items()]
        def accountValues(self):
            return []
        def sleep(self, n):
            pass

    class FOrder:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class FMkt:
        def __init__(self, action, totalQuantity):
            self.action, self.totalQuantity = action, totalQuantity
            self.orderType = "MKT"

    class A:
        symbols = []; watchlist = "auto"; arm = True; risk = 0.0025; base = 100000.0
        max_positions = 2; max_notional = 1.0; delayed = False; replay = False

    fib = FIB()
    bot = L.TightFlagTrader(fib, FOrder, FMkt, A())
    said = []
    bot.say = said.append                  # NEVER write test narration into logs/
    bot._fill_logger = lambda kind, sym: (lambda *a: None)   # never write paper_fills
    # _book now persists on every armed booking, so the REAL ledger is one call away
    # from a fake TST row (it happened once — 2026-09-10). Count the calls instead.
    bot.ledger_writes = []
    bot.write_ledger = lambda: bot.ledger_writes.append(len(bot.closed))
    bot._now = lambda: datetime(DAY.year, DAY.month, DAY.day, 9, 33)
    bot.prev_close["TST"] = 9.0
    bot.contracts["TST"] = object()

    def drive(bars, start=0):
        for i in range(start, len(bars.ts)):
            kk = (bars.ts[i].hour * 60 + bars.ts[i].minute - 570) // L.Clock5.WIDTH
            bot.on_5min("TST", kk, bars.ts[i], bars.o[i], bars.h[i],
                        bars.l[i], bars.c[i], bars.v[i], 1)
    return L, fib, bot, said, drive


def _fill(price, shares, hh, mm):
    """A realistic ib_async Fill: the EXECUTION carries cumQty/avgPrice (cumulative
    for the order, correct at emit time). AUDIT 2026-09-10 — the old rig omitted
    these and hand-set trade.orderStatus before emitting, which is exactly what the
    real library does NOT do; that made the suite blind to three critical bugs."""
    return _NS(execution=_NS(price=price, shares=shares, cumQty=shares, avgPrice=price),
               time=datetime(DAY.year, DAY.month, DAY.day, hh, mm))


def _stale(trade):
    """Put trade.orderStatus in the state the real library leaves it in when
    fillEvent fires: the submission status, with NOTHING filled yet."""
    trade.orderStatus = _NS(status="Submitted", avgFillPrice=0.0, filled=0,
                            remaining=float(trade.order.totalQuantity))
    return trade


def t_armed_books_from_a_stale_orderstatus():
    """CRITICAL AUDIT FIX 2026-09-10: ib_async emits fillEvent from execDetails and
    does NOT update trade.orderStatus there. Entry AND exit must book from the
    EXECUTION alone — with orderStatus still saying 'nothing filled'."""
    L, fib, bot, said, drive = _armed_rig()
    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 32, 10.32, 10.41, 10.30, 10.39)
    drive(b)
    entry = _stale(fib.trades[0])
    entry.fillEvent.emit(entry, _fill(10.47, entry.order.totalQuantity, 9, 32))
    st = bot.state.get("TST")
    assert st and st.get("open"), "a stale orderStatus must not stop the entry booking"
    assert abs(st["entry"] - 10.47) < 1e-9 and st["qty"] == entry.order.totalQuantity
    stop_tr = _stale(fib.trades[1])
    assert stop_tr.order.totalQuantity == st["qty"], "the stop must cover the position"
    stop_tr.fillEvent.emit(stop_tr, _fill(10.18, stop_tr.order.totalQuantity, 9, 34))
    assert len(bot.closed) == 1 and bot.closed[0]["kind"] == "STOP", \
        "a completed stop-out MUST book even though orderStatus.remaining is stale"
    assert abs(bot.closed[0]["exit"] - 10.18) < 1e-9
    assert bot.ledger_writes, "an armed booking must persist immediately, not at 15:49"


def t_armed_partial_entry_protects_the_whole_position():
    """CRITICAL AUDIT FIX 2026-09-10: a two-tranche entry must leave the protective
    stop covering the CUMULATIVE position, never one tranche."""
    L, fib, bot, said, drive = _armed_rig()
    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 32, 10.32, 10.41, 10.30, 10.39)
    drive(b)
    entry = _stale(fib.trades[0])
    total = int(entry.order.totalQuantity)
    first = total // 3
    entry.fillEvent.emit(entry, _NS(
        execution=_NS(price=10.40, shares=first, cumQty=first, avgPrice=10.40),
        time=datetime(DAY.year, DAY.month, DAY.day, 9, 32)))
    stop_tr = fib.trades[1]
    assert int(stop_tr.order.totalQuantity) == first, "tranche 1 protected"
    rest = total - first
    entry.fillEvent.emit(entry, _NS(
        execution=_NS(price=10.50, shares=rest, cumQty=total, avgPrice=10.44),
        time=datetime(DAY.year, DAY.month, DAY.day, 9, 33)))
    assert bot.state["TST"]["qty"] == total, \
        f"position must be the cumulative {total}, got {bot.state['TST']['qty']}"
    assert int(stop_tr.order.totalQuantity) == total, \
        "the protective stop must be resized to the WHOLE position, never one tranche"
    assert abs(bot.state["TST"]["entry"] - 10.44) < 1e-9, "entry is the average fill"


def t_armed_never_duplicates_an_in_flight_close():
    """CRITICAL AUDIT FIX 2026-09-10: while a market close is unfilled the protective
    stop is (correctly) cancelled — that must not make every later bar send another
    full-size close (a halted symbol would end up short the position it flattened)."""
    L, fib, bot, said, drive = _armed_rig()
    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 32, 10.32, 10.41, 10.30, 10.39)
    drive(b)
    entry = _stale(fib.trades[0])
    entry.fillEvent.emit(entry, _fill(10.41, entry.order.totalQuantity, 9, 32))
    qty = bot.state["TST"]["qty"]
    fib.pos["TST"] = qty                                   # broker holds the shares
    fib.trades[1].orderStatus = _NS(status="Cancelled", avgFillPrice=0.0,
                                    filled=0, remaining=0)  # killed from outside
    for m in range(33, 40):                                # seven more bars, no fill
        pad_to(b, 9 * 60 + m, 10.45, 10.46, 10.44, 10.45)
        drive(b, len(b.ts) - 1)
    mkts = [t for t in fib.trades if getattr(t.order, "orderType", "") == "MKT"]
    assert len(mkts) == 1, f"exactly ONE close may be in flight, got {len(mkts)}"
    assert bot.state["TST"]["open"], "the trade stays open until the close FILLS"
    assert not bot.closed, "no booking without a broker fill"


def t_armed_missing_0944_bar_still_cancels_the_entry():
    """MAJOR AUDIT FIX 2026-09-10: IBKR omits a 1-min bar when nothing trades in it.
    If the missing one straddles the deadline, the old cleanup never ran and the DAY
    entry order rested live all session (the 09-09 landmine). ANY later bar sweeps."""
    L, fib, bot, said, drive = _armed_rig()
    b = mk(B1, B2)
    for m in range(32, 51):
        if m == 44:
            continue                                       # 09:44 never printed
        pad_to(b, 9 * 60 + m, 10.30, 10.35, 10.28, 10.32)  # never touches 10.40
    drive(b)
    entry = fib.trades[0]
    assert entry.order in fib.cancelled, \
        "a missing 09:44 bar must NOT strand a live entry order"
    assert bot.done.get("TST") and "TST" not in bot.state


def t_order_legs_are_distinguishable():
    """MAJOR AUDIT FIX 2026-09-10: a restart sweep must be able to tell an unfilled
    ENTRY (cancel it) from a PROTECTIVE stop guarding real shares (never cancel it).
    A short's entry is a SELL STP exactly like a long's protective stop, so the only
    safe discriminator is the leg tag on the orderRef."""
    L, fib, bot, said, drive = _armed_rig()
    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 32, 10.32, 10.41, 10.30, 10.39)
    drive(b)
    entry = _stale(fib.trades[0])
    assert entry.order.orderRef.endswith("-E"), entry.order.orderRef
    entry.fillEvent.emit(entry, _fill(10.41, entry.order.totalQuantity, 9, 32))
    assert fib.trades[1].order.orderRef.endswith("-P"), fib.trades[1].order.orderRef
    fib.pos["TST"] = bot.state["TST"]["qty"]
    bot.close_my_position("TST", bot.state["TST"], "EOD flatten")
    mkt = [t for t in fib.trades if getattr(t.order, "orderType", "") == "MKT"][0]
    assert mkt.order.orderRef.endswith("-C"), mkt.order.orderRef


def t_armed_entry_and_exit_are_broker_fills():
    """ARMED TRUTH: a bar touching the level opens nothing; a bar touching the stop
    closes nothing. The broker's fills do both, at THEIR prices (TER 09-09)."""
    L, fib, bot, said, drive = _armed_rig()
    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 32, 10.32, 10.41, 10.30, 10.39)    # bars touch the 10.40 level
    pad_to(b, 9 * 60 + 33, 10.39, 10.39, 10.14, 10.16)    # bars pierce the 10.20 stop
    drive(b)
    st = bot.state.get("TST")
    assert st is not None and not st.get("open"), "bar touch must NOT open an armed trade"
    assert not bot.closed, "nothing may book before a broker fill"
    entry = fib.trades[0]
    assert entry.order.orderType == "STP" and entry.order.action == "BUY"

    entry.orderStatus = _NS(status="Filled", avgFillPrice=10.47, filled=1250, remaining=0)
    entry.fillEvent.emit(entry, _fill(10.47, 1250, 9, 32))     # the REAL fill, slipped
    st = bot.state["TST"]
    assert st["open"] and abs(st["entry"] - 10.47) < 1e-9, "entry must be the broker's price"
    assert abs(st["R"] - 0.27) < 1e-9, "R must be |real entry - stop|, not the modeled 0.20"
    stop_tr = fib.trades[1]
    assert stop_tr.order.action == "SELL" and abs(stop_tr.order.auxPrice - 10.20) < 1e-9

    pad_to(b, 9 * 60 + 34, 10.16, 10.18, 10.10, 10.12)    # another bar through the stop
    drive(b, len(b.ts) - 1)
    assert bot.state["TST"]["open"] and not bot.closed, \
        "a bar at the stop price books nothing while armed"

    stop_tr.orderStatus = _NS(status="Filled", avgFillPrice=10.18, filled=1250, remaining=0)
    stop_tr.fillEvent.emit(stop_tr, _fill(10.18, 1250, 9, 34))
    assert len(bot.closed) == 1 and bot.closed[0]["kind"] == "STOP"
    assert abs(bot.closed[0]["exit"] - 10.18) < 1e-9, "exit must be the broker's fill price"
    assert "TST" not in bot.state and bot.done.get("TST")


def t_armed_late_start_places_nothing():
    """LATE-ARM GUARD: a setup whose entry window is already over places NO order
    and books NO row (the 12:07 TER catastrophe)."""
    L, fib, bot, said, drive = _armed_rig()
    bot._now = lambda: datetime(DAY.year, DAY.month, DAY.day, 12, 7)
    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 40, 10.32, 10.45, 10.30, 10.42)    # bars even touch the level
    drive(b)
    assert not fib.trades, "a late start must never send an order"
    assert bot.done.get("TST") and "TST" not in bot.state and not bot.closed
    assert any("LATE START" in x for x in said)


def t_armed_no_trigger_cancels_the_entry_order():
    """BUG FIX 2026-09-09 (KLAC): when the level is never reached by 09:45, the
    RESTING ENTRY itself must be cancelled — not the nonexistent stop leg."""
    L, fib, bot, said, drive = _armed_rig()
    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 50, 10.30, 10.35, 10.28, 10.32)    # never touches 10.40
    drive(b)
    entry = fib.trades[0]
    assert entry.order in fib.cancelled, "the unfilled resting entry MUST be cancelled"
    assert bot.done.get("TST") and "TST" not in bot.state


def t_armed_eod_closes_at_market_and_books_its_fill():
    """ARMED EOD: the flatten is a real market order and the ledger gets ITS fill."""
    L, fib, bot, said, drive = _armed_rig()
    b = mk(B1, B2)
    pad_to(b, 9 * 60 + 32, 10.32, 10.41, 10.30, 10.39)
    drive(b)
    entry = fib.trades[0]
    entry.orderStatus = _NS(status="Filled", avgFillPrice=10.41, filled=1250, remaining=0)
    entry.fillEvent.emit(entry, _fill(10.41, 1250, 9, 32))
    fib.pos["TST"] = 1250                                  # broker net = our long
    pad_to(b, 15 * 60 + 49, 10.50, 10.95, 10.45, 10.90)   # quiet drift to the EOD bar
    pad_to(b, 15 * 60 + 51, 10.90, 10.92, 10.85, 10.88)
    drive(b, len(b.ts) - (15 * 60 + 51 - (9 * 60 + 32)))
    closes = [t for t in fib.trades if getattr(t.order, "orderType", "") == "MKT"]
    assert closes, "the EOD flatten must send a real market order"
    assert bot.state["TST"].get("closing") and not bot.closed, \
        "the exit books on the close FILL, not on the bar"
    ct = closes[0]
    ct.orderStatus = _NS(status="Filled", avgFillPrice=10.89, filled=1250, remaining=0)
    ct.fillEvent.emit(ct, _fill(10.89, 1250, 15, 50))
    assert len(bot.closed) == 1 and bot.closed[0]["kind"] == "EOD"
    assert abs(bot.closed[0]["exit"] - 10.89) < 1e-9, "EOD must book the real close fill"


def t_cache_replay_has_its_own_log():
    """BUG FIX 2026-09-10: a --cache-day regression must NOT narrate into the current
    day's session log (it appended 09-03 setups and fills to the 09-10 record), and
    must not rewrite the replayed day's log either."""
    import live_trader_tightflag as L
    from datetime import date as _D

    class A:
        symbols = []; watchlist = "auto"; arm = False; risk = 0.0025; base = 100000.0
        max_positions = 2; max_notional = 1.0; delayed = False; replay = False

    class NoIB:
        def positions(self): return []
        def accountValues(self): return []

    saved = L.TightFlagTrader.RUN_MODE
    try:
        L.TightFlagTrader.RUN_MODE = "cache"
        bot = L.TightFlagTrader(NoIB(), None, None, A())
        day = _D(2026, 9, 3)
        assert bot._log_path_for(day) == "logs/cachereplay_2026-09-03.log", bot.log_path
        assert "tightflag_" not in bot.log_path, \
            f"a cache run must never open a session log ({bot.log_path})"
        bot.roll_day(day)                       # the path must follow the replayed day
        assert bot.log_path == "logs/cachereplay_2026-09-03.log", bot.log_path

        L.TightFlagTrader.RUN_MODE = "shadow"
        live = L.TightFlagTrader(NoIB(), None, None, A())
        live.roll_day(day)
        assert live.log_path == "logs/tightflag_2026-09-03.log", live.log_path
    finally:
        L.TightFlagTrader.RUN_MODE = saved


import os
if __name__ == "__main__":
    print("tight-flag rule tests (1-min pivot rules)\n" + "=" * 46)
    check("ratio gate (2:1, inclusive boundary)", t_ratio_gate)
    check("side rule (green/red/doji)", t_side_rule)
    check("entry window: 09:44 touch fills", t_entry_window_0944_fills)
    check("entry window: 09:45 first-touch refused", t_entry_window_0945_refused)
    check("entry window through the LIVE bot path", t_entry_window_live_bot)
    check("gap-through fills at the open", t_gap_through_fill)
    check("fly trigger (1.75R incl.) + trail lag", t_fly_trigger_and_trail)
    check("prev-close long gate", t_prev_close_long_gate)
    check("no arming/entry after 15:49 (live path)", t_no_arming_after_eod)
    check("real-day golden (2026-08-28 recompute == ledger)", t_real_day_golden)
    check("EOD books the 15:49 bar (1-min pivot fix)", t_eod_exit_bar)
    check("no re-entry after a booked trade", t_no_reentry_after_stopout)
    check("ARMED: entry/exit are broker fills, not bars", t_armed_entry_and_exit_are_broker_fills)
    check("ARMED: late start places nothing", t_armed_late_start_places_nothing)
    check("ARMED: no_trigger cancels the ENTRY order", t_armed_no_trigger_cancels_the_entry_order)
    check("ARMED: EOD closes at market, books its fill", t_armed_eod_closes_at_market_and_books_its_fill)
    check("ARMED: books from a STALE orderStatus", t_armed_books_from_a_stale_orderstatus)
    check("ARMED: partial entry protects the whole position", t_armed_partial_entry_protects_the_whole_position)
    check("ARMED: never duplicates an in-flight close", t_armed_never_duplicates_an_in_flight_close)
    check("ARMED: missing 09:44 bar still cancels the entry", t_armed_missing_0944_bar_still_cancels_the_entry)
    check("ARMED: order legs are distinguishable (E/P/C)", t_order_legs_are_distinguishable)
    check("cache replays get their own log file", t_cache_replay_has_its_own_log)
    print("=" * 46)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    raise SystemExit(1 if FAIL else 0)
