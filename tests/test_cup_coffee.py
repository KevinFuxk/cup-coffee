"""
tests/test_cup_coffee.py — the safety net for the CURRENT (v2, rolling-rim) rules
=================================================================================
One tiny hand-built price pattern per rule, each with a known correct answer.
Run after ANY change to pattern_detector.py / live_trader_ibkr.py / research_data.py:

    python tests/test_cup_coffee.py

If a change accidentally breaks a rule you didn't mean to touch, a test fails HERE
before the live bot trades it wrong. (The old test_detector.py tested the pre-v2
ATR detector and is archived.)

What is covered, mapped to the gate numbers in what_the_code_actually_does.md §2:
  golden cup            gates 1-13 end to end: exactly one entry, at the right bar & price
  cup band              gate 4: right rim must recover to within 25% of cup depth
  rim line              gate 5: an interior bar strictly above the chord kills the cup;
                        a bar exactly ON the chord does not (1e-9 tolerance)
  symmetry loose/strict gate 7: same shape passes "max" (live) and fails "min"
  handle depth          gate 10: pullback > 20% of cup -> dead
  rolling rim           gate 9: early break DETHRONES (no entry off the old rim),
                        entry comes off the NEW peak-confirmed rim; a TIE does not dethrone
  4-bar floor           gate 11: earliest possible entry is handle bar 4
  15:49 cutoff          gate 13: same setup too late in the day -> dropped
  labeler conventions   stop-before-target on one bar, MFE on highs, timeout at close
  scan/detect parity    the live pre-armer and the frozen detector agree bar-for-bar
  aggregator            2-min clock-aligned buckets, gap handling
  watchlist parser      raw TradingView export -> tickers
  real-day golden       re-detect one cached SPY day, must match the recorded pile
  ORDER STATE (armed)   'entered' = IBKR's fill count (grace bar, then MISSED FILL, never a ghost);
                        re-place under a new ref, never after a fill, fill-race kills the successor;
                        own-book share counts per ref (partial exits stay on the book);
                        flatten = MY orders + MY book only, routed SMART, verified;
                        watchdog = own book, live children only, repairs inside the OCA group
"""
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)

import json
import traceback
from datetime import date as Date, datetime, timedelta

from cup_coffee_config_v2 import CONFIG
from data_layer import Bars
from pattern_detector import PatternDetector
from research_data import label_full_path

DAY = Date(2026, 8, 20)


def bars_from_hl(hl, start_min=0, day=DAY):
    """Build a Bars series from (high, low) pairs; open/close = midpoint, 1-min from 09:30."""
    b = Bars(symbol="TEST", date=day, timeframe="1min", ts=[], o=[], h=[], l=[], c=[], v=[])
    for i, (h, l) in enumerate(hl):
        t = datetime.combine(day, datetime.min.time()) + timedelta(minutes=9 * 60 + 30 + start_min + i)
        mid = round((h + l) / 2, 4)
        b.ts.append(t); b.o.append(mid); b.h.append(h); b.l.append(l); b.c.append(mid); b.v.append(1000)
    return b


def golden_hl(rim=99.8, tie_bar=None, deep_handle=False, obstruct=None, late_lip=None):
    """The canonical valid cup+handle (left rim idx2 @100, bottom @96, right rim idx22,
    entry on handle bar 4 = idx25). Keyword tweaks turn it into each failure case."""
    hl = [(98.0, 97.6), (99.0, 98.6), (100.0, 99.5)]                     # idx 0,1,2(left rim)
    down = [99.4, 99.0, 98.6, 98.2, 97.8, 97.4, 97.0, 96.6, 96.3]        # idx 3..11
    up = [96.6, 97.0, 97.4, 97.8, 98.2, 98.6, 99.0, 99.2, 99.4]         # idx 13..21
    hl += [(h, h - 0.4) for h in down]
    hl += [(96.2, 96.0)]                                                  # idx 12 = bottom
    hl += [(h, h - 0.4) for h in up]
    hl += [(rim, rim - 0.4)]                                              # idx 22 = right rim
    hl += [(99.5, 99.3), (99.4, 99.2), (99.9, 99.5)]                     # handle 1,2 + entry bar (idx 25)
    hl += [(99.5, 99.3)] * 4                                              # quiet tail
    if tie_bar is not None:
        hl[24] = (tie_bar, tie_bar - 0.2)
    if deep_handle:
        hl[24] = (99.4, 98.9)                                             # dip 0.9 > 0.76 cap
        hl[25] = (99.5, 99.3)                                             # and no breakout after
    if obstruct is not None:
        hl[15] = (obstruct, obstruct - 0.4)                               # interior bar vs the chord
    if late_lip is not None:                                              # blunt the recovery leg
        hl = hl[:13] + [(h, h - 0.4) for h in
                        [96.5, 96.9, 97.3, 97.7, 98.0, 98.3, 98.5, 98.7, 98.8]] \
             + [(98.9, 98.5)] + [(98.5, 98.1)] * 8
    return hl


def det(mode="max"):
    cfg = {**CONFIG, "pattern": {**CONFIG["pattern"], "rim_symmetry": mode}}
    return PatternDetector(cfg)


# --------------------------------------------------------------------------- tests
def test_golden_cup():
    ev = det().detect(bars_from_hl(golden_hl()), "TEST", DAY, signals_only=True)
    assert len(ev) == 1, f"expected 1 event, got {len(ev)}"
    e = ev[0]
    assert e.cup_left_idx == 2 and e.cup_right_idx == 22, (e.cup_left_idx, e.cup_right_idx)
    assert e.breakout_idx == 25, f"entry must be handle bar 4 (idx 25), got {e.breakout_idx}"
    assert abs(e.entry_price - 99.81) < 1e-9, e.entry_price          # rim + $0.01
    assert abs(e.stop_price - 99.2) < 1e-9, e.stop_price             # handle low
    assert abs(e.risk_R - 0.61) < 1e-9, e.risk_R


def test_cup_band_recovery_fail():
    # recovery tops out at 98.9 < left(100) - 0.25*depth(4) = 99.0 -> no valid right rim
    ev = det().detect(bars_from_hl(golden_hl(late_lip=True)), "TEST", DAY, signals_only=True)
    assert ev == [], f"sub-band recovery must produce no trade, got {len(ev)}"


def test_rim_line_obstruction():
    # chord (2,100)->(22,99.8); line at idx15 = 99.87. Above it -> cup dead.
    ev = det().detect(bars_from_hl(golden_hl(obstruct=100.2)), "TEST", DAY, signals_only=True)
    assert ev == [], "interior bar above the rim line must kill the cup"
    # exactly ON the line -> NOT obstructed (strictly-above rule), golden entry survives
    ev = det().detect(bars_from_hl(golden_hl(obstruct=99.87)), "TEST", DAY, signals_only=True)
    assert len(ev) == 1 and abs(ev[0].entry_price - 99.81) < 1e-9


def test_rim_symmetry_loose_vs_strict():
    # rim 99.025: |100-99.025|=0.975 -> loose bound 0.25*max(4,3.025)=1.0 PASS,
    #                                   strict bound 0.25*min(...)=0.756 FAIL
    hl = golden_hl(rim=99.025)
    hl[23], hl[24], hl[25] = (98.9, 98.7), (98.8, 98.6), (99.1, 98.9)    # handle under the low rim
    up = [96.6, 96.9, 97.2, 97.5, 97.8, 98.1, 98.4, 98.6, 98.6]         # keep interior under the chord
    for i, h in enumerate(up):
        hl[13 + i] = (h, h - 0.4)
    assert len(det("max").detect(bars_from_hl(hl), "TEST", DAY, signals_only=True)) == 1
    assert det("min").detect(bars_from_hl(hl), "TEST", DAY, signals_only=True) == []


def test_handle_too_deep():
    ev = det().detect(bars_from_hl(golden_hl(deep_handle=True)), "TEST", DAY, signals_only=True)
    assert ev == [], "handle deeper than 20% of the cup must kill the setup"


def test_rolling_rim_dethrone():
    # handle bar 2 (idx24) breaks the rim BEFORE the 4-bar floor -> DETHRONE, not entry.
    # idx24 (99.85) becomes the new peak-confirmed rim; entry = its bar 4 (idx27) @ 99.86.
    hl = golden_hl()
    hl[24] = (99.85, 99.6)
    hl[25], hl[26], hl[27], hl[28] = (99.7, 99.5), (99.6, 99.4), (99.95, 99.7), (99.6, 99.4)
    ev = det().detect(bars_from_hl(hl), "TEST", DAY, signals_only=True)
    assert len(ev) == 1, f"expected 1 event off the ROLLED rim, got {len(ev)}"
    e = ev[0]
    assert e.cup_right_idx == 24, f"rim must have rolled to idx24, got {e.cup_right_idx}"
    assert e.breakout_idx == 27 and abs(e.entry_price - 99.86) < 1e-9, (e.breakout_idx, e.entry_price)


def test_tie_does_not_dethrone():
    # handle bar 2 EQUALS the rim (99.8): strict '>' means no dethrone; golden entry stands
    ev = det().detect(bars_from_hl(golden_hl(tie_bar=99.8)), "TEST", DAY, signals_only=True)
    assert len(ev) == 1 and ev[0].cup_right_idx == 22 and abs(ev[0].entry_price - 99.81) < 1e-9


def test_eod_cutoff():
    # same golden pattern started so the entry bar lands at 15:50 -> dropped...
    late = det().detect(bars_from_hl(golden_hl(), start_min=355), "TEST", DAY, signals_only=True)
    assert late == [], "entry bar after 15:49 must be dropped"
    # ...two minutes earlier the entry bar is 15:48 -> taken
    ok = det().detect(bars_from_hl(golden_hl(), start_min=353), "TEST", DAY, signals_only=True)
    assert len(ok) == 1


def test_labeler_stop_beats_target():
    # post-entry bar touches BOTH the stop and the 1R target -> the stop wins (-1R)
    b = bars_from_hl([(100.0, 99.8), (100.5, 100.1), (101.2, 98.9), (100.0, 99.8)])
    lab = label_full_path(b, 0, entry=100.0, stop=99.0, risk=1.0, take_profits=(1,))
    assert lab["realized_R"]["1"] == -1.0, lab["realized_R"]
    assert lab["stopped"] is True


def test_labeler_mfe_and_timeout():
    # hits 2R (high 102.1), never 3R, never stopped -> 3R books the closing print (1.5R)
    b = bars_from_hl([(100.0, 99.8), (100.5, 100.1), (102.1, 101.0), (101.6, 101.4)])
    b.c[-1] = 101.5
    lab = label_full_path(b, 0, entry=100.0, stop=99.0, risk=1.0, take_profits=(1, 2, 3))
    r = lab["realized_R"]
    assert r["1"] == 1.0 and r["2"] == 2.0 and abs(r["3"] - 1.5) < 1e-9, r
    assert abs(lab["full_mfe_R"] - 2.1) < 1e-9, lab["full_mfe_R"]


def test_scan_matches_detector():
    from live_trader_ibkr import scan_setups
    d = det()
    full = bars_from_hl(golden_hl())
    scan = scan_setups(d, full)
    st = scan.get(22)
    assert st and st["state"] == "entered" and st["entry_bar"] == 25, st
    assert abs(st["trigger"] - 99.81) < 1e-9 and abs(st["stop"] - 99.2) < 1e-9
    # truncate to the close of handle bar 3 (idx 24): the live bot must be ARMABLE now,
    # so the resting order exists before bar 4 opens — the backtest-faithful timing
    part = bars_from_hl(golden_hl()[:25])
    st = scan_setups(d, part).get(22)
    assert st and st["state"] == "forming", st
    assert len(part) >= st["earliest"], "order must be armable at the close of handle bar 3"
    assert abs(st["trigger"] - 99.81) < 1e-9 and abs(st["stop"] - 99.2) < 1e-9


def test_minute_aggregator():
    from live_trader_ibkr import MinuteAggregator
    out = []
    agg = MinuteAggregator(2, lambda *a: out.append(a))
    t0 = datetime.combine(DAY, datetime.min.time()) + timedelta(minutes=9 * 60 + 30)
    feed = [(0, 10, 11, 9, 10.5), (1, 10.5, 12, 10, 11), (2, 11, 13, 11, 12),   # 09:33 missing
            (4, 12, 12.5, 11.5, 12), (5, 12, 14, 12, 13)]
    for m, o, h, l, c in feed:
        agg.add(t0 + timedelta(minutes=m), o, h, l, c, 100)
    agg._flush()
    assert len(out) == 3, out
    ts, o, h, l, c, v = out[0]                        # 09:30 bucket = minutes 0+1
    assert ts.minute == 30 and o == 10 and h == 12 and l == 9 and c == 11 and v == 200
    ts, o, h, l, c, v = out[1]                        # 09:32 bucket = minute 2 alone (gap-flushed)
    assert ts.minute == 32 and h == 13 and v == 100
    assert out[2][0].minute == 34                     # 09:34 bucket = minutes 4+5


def test_watchlist_parser():
    from live_trader_ibkr import read_watchlist
    p = "/tmp/wl_test_cupcoffee.txt"
    open(p, "w").write('###INDEX,NASDAQ:QQQ,AMEX:SPY,###THE FLY,NASDAQ:NVDA,nasdaq:nvda,TVC:VIX\n# note\nAMD\n')
    syms, _ = read_watchlist(None, p)
    assert syms == ["QQQ", "SPY", "NVDA", "VIX", "AMD"], syms


def test_real_day_golden():
    """Re-detect one cached SPY 5-min day; must reproduce the recorded pile exactly."""
    pile_p, cache_d = "data/events_cup_ibkr5.jsonl", "cache/ibkr5/SPY"
    if not (_os.path.exists(pile_p) and _os.path.isdir(cache_d)):
        print("   (skipped — cached data not present)")
        return
    from zoneinfo import ZoneInfo
    from datetime import time as dtime
    ET = ZoneInfo("America/New_York")
    rows = [json.loads(l) for l in open(pile_p) if l.strip()]
    spy_days = sorted({r["day"] for r in rows if r["symbol"] == "SPY"})
    day = spy_days[len(spy_days) // 2]                 # a mid-history day, not an edge case
    want = sorted((r["breakout_idx"], round(r["entry_price"], 4))
                  for r in rows if r["symbol"] == "SPY" and r["day"] == day)
    b = Bars(symbol="SPY", date=Date.fromisoformat(day), timeframe="5min",
             ts=[], o=[], h=[], l=[], c=[], v=[])
    for x in json.load(open(f"{cache_d}/{day}.json")):
        t = datetime.fromtimestamp(x["t"] / 1000, tz=ET).replace(tzinfo=None)
        if dtime(9, 30) <= t.time() <= dtime(15, 59):
            b.ts.append(t); b.o.append(x["o"]); b.h.append(x["h"]); b.l.append(x["l"])
            b.c.append(x["c"]); b.v.append(x.get("v", 0))
    # the reference pile was built under the RETIRED caps (cup/handle <= 60); pin them
    # here so this test keeps proving DETECTOR-CORE reproducibility, not the live config
    dcap = PatternDetector({**CONFIG, "pattern": {**CONFIG["pattern"],
                                                  "cup_max_bars": 60, "handle_max_bars": 60}})
    got = sorted((e.breakout_idx, round(e.entry_price, 4))
                 for e in dcap.detect(b, "SPY", b.date, signals_only=True))
    assert got == want, f"SPY {day}: pile {want} vs re-detect {got}"
    print(f"   (SPY {day}: {len(got)} event(s) reproduced exactly)")


def _mini_trader(replay: bool):
    """A Trader wired to dummies — just enough to exercise reconcile()'s arm path."""
    from argparse import Namespace
    from live_trader_ibkr import Trader

    class DummyIB:
        def accountValues(self): return []
        def positions(self): return []
        def openTrades(self): return []
    a = Namespace(replay=replay, arm=False, minstop=0.0, risk=0.01, base=10000,
                  tp=6.0, max_positions=5)
    tr = Trader(DummyIB(), None, None, a, agg_ks=[])
    tr._report = True
    tr.log_path = "/dev/null"          # tests must NEVER write into the real live logs
    return tr


def test_no_arming_after_eod_flatten():
    """The 2026-08-24 audit hole: the 15:49 flatten is followed by the aggregators'
    final 2/5-min buckets (stamped < 15:49) reaching reconcile — LIVE must refuse to
    arm a fresh bracket there, or a DAY buy-stop rests 15:49-16:00 with no flatten
    behind it and a fill survives overnight."""
    b = bars_from_hl(golden_hl()[:25])              # armable forming setup (bar-3 close)
    tr = _mini_trader(replay=False)
    tr.eod_done = True                              # the day's flatten already fired
    tr.reconcile("TEST", "1min", b)
    assert tr.pending == {}, "live armed a bracket AFTER the EOD flatten — overnight risk"
    tr.eod_done = False                             # sanity: same call arms during the day
    tr.reconcile("TEST", "1min", b)
    assert "TEST" in tr.pending and abs(tr.pending["TEST"]["trigger"] - 99.81) < 1e-9


def test_replay_still_arms_after_first_symbols_eod():
    """The July trap: replay walks symbols sequentially, so a later symbol's whole day
    arrives with eod_done already True — replay must KEEP arming (shadow-only)."""
    b = bars_from_hl(golden_hl()[:25])
    tr = _mini_trader(replay=True)
    tr.eod_done = True                              # set by the previous symbol's 15:49
    tr.reconcile("TEST", "1min", b)
    assert "TEST" in tr.pending, "the eod_done guard must never block replay's later symbols"


def test_tv_export_recognized_by_content_not_name():
    """2026-09-02: the export was named '9_2_2026.txt' — no keyword — and the bot fell
    back to a stale list. Exports must be recognized by their content."""
    from live_trader_ibkr import is_tv_export
    p = "/tmp/9_2_2026_test.txt"
    open(p, "w").write("###INDEX,NASDAQ:QQQ,AMEX:SPY,###CNBC,NASDAQ:NVDA,NYSE:DE")
    assert is_tv_export(p), "a real TradingView export must be recognized whatever its name"
    open(p, "w").write("Dear diary, today the market was volatile and I felt unsure.")
    assert not is_tv_export(p), "prose must never be mistaken for a watchlist"
    open(p, "w").write("NVDA\nAMD\n")
    assert not is_tv_export(p), "a bare hand-typed list is not a TradingView export"


def test_hot_add_is_additive_and_fresh_only():
    """The Fly publishes at 09:55: a re-export mid-session must ADD its names to the
    running bot — never remove one (it may hold a position), never from a stale file."""
    import os, time
    from live_trader_ibkr import watchlist_additions
    p = "/tmp/hotadd_test.txt"
    open(p, "w").write("###CNBC,NASDAQ:NVDA,###THE FLY,NASDAQ:DPZ,NYSE:LVS")
    today = datetime.now().date()
    assert watchlist_additions({"NVDA", "AAPL"}, p, today) == ["DPZ", "LVS"]   # AAPL not removed
    assert watchlist_additions({"NVDA", "DPZ", "LVS"}, p, today) == []
    old = time.time() - 2 * 86400
    os.utime(p, (old, old))                                                    # yesterday's export
    assert watchlist_additions({"NVDA"}, p, today) == [], "a stale export must inject nothing"
    assert watchlist_additions({"NVDA"}, None, today) == []


def test_scan_memo_is_exactly_equivalent_on_a_real_day():
    """The speed memo must never change a decision: memoized incremental scans over a
    real cached 15s day must equal the full scan at every sampled prefix."""
    from live_trader_ibkr import scan_setups
    from datetime import time as dtime
    from zoneinfo import ZoneInfo
    import glob
    files = sorted(glob.glob("cache/ibkr15s/*/2026-09-01.json"))
    if not files:
        print("   (skipped — no cached 15s day)")
        return
    p = [f for f in files if "/DUOL/" in f] or files
    rows = json.load(open(p[0]))
    ET = ZoneInfo("America/New_York")
    full = Bars(symbol="X", date=Date(2026, 9, 1), timeframe="15s", ts=[], o=[], h=[], l=[], c=[], v=[])
    for r in rows:
        tt = datetime.fromtimestamp(r["t"] / 1000, ET).replace(tzinfo=None)
        if dtime(9, 30) <= tt.time() <= dtime(15, 59):
            full.ts.append(tt); full.o.append(r["o"]); full.h.append(r["h"]); full.l.append(r["l"]); full.c.append(r["c"]); full.v.append(r.get("v", 0))
    d = det(); memo = {}
    checked = 0
    for n in range(25, len(full) + 1):
        pre = Bars(symbol="X", date=full.date, timeframe="15s", ts=full.ts[:n], o=full.o[:n],
                   h=full.h[:n], l=full.l[:n], c=full.c[:n], v=full.v[:n])
        inc = scan_setups(d, pre, memo)                # incremental, memo carried bar to bar
        if n % 37 == 0 or n == len(full):              # full re-scan is the slow reference
            assert inc == scan_setups(d, pre), f"memo diverged from the full scan at n={n}"
            checked += 1
    print(f"   (memo == full scan at {checked} prefixes of a {len(full)}-bar real day)")


def test_universe_rules_as_code():
    """The old screening rules, live: >= $15, common stock only, no commodity names."""
    from types import SimpleNamespace as NS
    from live_trader_ibkr import universe_verdict
    assert universe_verdict(36.7, NS(stockType="COMMON", industry="Technology", category="Semiconductors")) == ""
    assert "floor" in universe_verdict(13.53, NS(stockType="COMMON", industry="Consumer, Non-cyclical", category="Pharmaceuticals"))
    assert "not common" in universe_verdict(100.0, NS(stockType="ETF", industry="", category=""))
    assert "commodity" in universe_verdict(160.0, NS(stockType="COMMON", industry="Energy", category="Oil&Gas"))
    assert "commodity" in universe_verdict(128.0, NS(stockType="COMMON", industry="Basic Materials", category="Chemicals"))
    assert universe_verdict(15.06, NS(stockType="COMMON", industry="Energy", category="Energy-Alternate Sources")) == ""
    assert universe_verdict(None, None) == ""            # unknown price/details: never a false drop


class _Ev:
    """ib_async-style event: `tr.fillEvent += handler`."""
    def __init__(self): self.fs = []
    def __iadd__(self, f): self.fs.append(f); return self
    def fire(self, *a):
        for f in self.fs: f(*a)


def _armed_trader():
    """A Trader in --arm mode against a fake IBKR that records every order and never
    fills by itself: the tests decide what IBKR 'filled'."""
    import itertools
    from types import SimpleNamespace as NS
    import live_trader_ibkr as L
    L.FILLS_CSV = "/tmp/test_paper_fills.csv"      # never the real data/paper_fills.csv
    tr = _mini_trader(replay=False)
    tr.a.arm = True
    trades, cancelled, said = [], [], []
    tr._say = said.append

    class FakeTrade:
        def __init__(self, contract, order):
            self.contract, self.order = contract, order
            self.orderStatus = NS(status="Submitted", filled=0)
            self.fillEvent, self.log = _Ev(), []

    class FakeIB:
        client = NS(getReqId=itertools.count(100).__next__)
        def accountValues(self): return []
        def positions(self): return []
        def openTrades(self): return [t for t in trades if t.orderStatus.status in ("Submitted", "PreSubmitted")]
        def placeOrder(self, c, o):
            t = FakeTrade(c, o); trades.append(t); return t
        def cancelOrder(self, o):
            cancelled.append(o.orderRef)
            for t in trades:
                if t.order is o:
                    t.orderStatus.status = "Cancelled"
    tr.ib = FakeIB()
    tr.Order = lambda **kw: NS(**kw)
    tr.MarketOrder = lambda act, qty: NS(action=act, orderType="MKT", totalQuantity=qty, auxPrice=0, lmtPrice=0)
    tr.contracts = {"TEST": NS(symbol="TEST", exchange="SMART")}
    return tr, trades, cancelled, said


def test_armed_entered_is_what_ibkr_filled_not_what_the_bar_says():
    """ROOT CAUSE of the 2026-09-02 ALMS double position: the pending was popped on the
    BAR's say-so while the buy-stop still rested at IBKR (stops trigger on quotes, not on
    a print) -> a ghost bracket, then a second one armed on the next rim. Armed 'entered'
    must be IBKR's fill count: unfilled = one bar of grace, then cancel + MISSED FILL."""
    hl = golden_hl()
    # (a) the bar breaks out, IBKR never fills
    tr, trades, cancelled, said = _armed_trader()
    tr.reconcile("TEST", "1min", bars_from_hl(hl[:25]))            # arms at the bar-3 close
    assert "TEST" in tr.pending and len(trades) == 3
    ref = tr.pending["TEST"]["ref"]
    tr.reconcile("TEST", "1min", bars_from_hl(hl[:26]))            # bar 25 crosses the trigger
    assert "TEST" in tr.pending and cancelled == [], "grace: a fill can lag the bar by seconds"
    tr.reconcile("TEST", "1min", bars_from_hl(hl[:27]))            # still 0 filled -> never a ghost
    assert "TEST" not in tr.pending and cancelled == [ref], cancelled
    assert any("MISSED FILL" in l for l in said), said
    tr.reconcile("TEST", "1min", bars_from_hl(hl[:28]))
    assert len(trades) == 3, "no second bracket may appear after the missed fill"
    # (b) IBKR filled the whole entry -> settled, nothing cancelled
    tr, trades, cancelled, said = _armed_trader()
    tr.reconcile("TEST", "1min", bars_from_hl(hl[:25]))
    trades[0].orderStatus.filled = tr.pending["TEST"]["qty"]
    tr.reconcile("TEST", "1min", bars_from_hl(hl[:26]))
    assert "TEST" not in tr.pending and cancelled == [] and tr.entered_at[("TEST", "1min")] == 25
    # (c) partial fill -> grace, then the REMAINDER is cancelled and the fill is ours
    tr, trades, cancelled, said = _armed_trader()
    tr.reconcile("TEST", "1min", bars_from_hl(hl[:25]))
    ref = tr.pending["TEST"]["ref"]
    trades[0].orderStatus.filled = 5
    tr.reconcile("TEST", "1min", bars_from_hl(hl[:26]))
    assert "TEST" in tr.pending and cancelled == []
    tr.reconcile("TEST", "1min", bars_from_hl(hl[:27]))
    assert "TEST" not in tr.pending and cancelled == [ref] and any("PARTIAL" in l for l in said)


def test_replace_uses_a_new_ref_and_never_after_a_fill():
    """IBKR 10326: OCA children cannot be revised, so a deeper handle = cancel + re-place.
    The re-place must carry a NEW ref (= new OCA group), keep the old ref's levels as a
    snapshot, be skipped once anything filled, and — if the old parent fills inside the
    cancel's round trip — the successor must be cancelled by the fill itself."""
    from types import SimpleNamespace as NS
    hl = golden_hl()
    hl[25] = (99.4, 99.1)                                           # handle deepens instead of breaking
    tr, trades, cancelled, said = _armed_trader()
    tr.reconcile("TEST", "1min", bars_from_hl(hl[:25]))
    ref1 = tr.pending["TEST"]["ref"]
    assert abs(tr.pending["TEST"]["stop"] - 99.2) < 1e-9
    tr.reconcile("TEST", "1min", bars_from_hl(hl[:26]))
    p = tr.pending["TEST"]
    assert cancelled == [ref1] and p["ref"] == ref1 + "-r2", (cancelled, p["ref"])
    assert abs(p["stop"] - 99.1) < 1e-9 and len(trades) == 6
    assert p["trades"]["TP"].order.ocaGroup == p["ref"] and p["trades"]["SL"].order.orderRef == p["ref"]
    assert abs(tr.brackets[ref1]["stop"] - 99.2) < 1e-9, "the old ref keeps the levels it was placed at"
    assert abs(tr.brackets[p["ref"]]["stop"] - 99.1) < 1e-9
    # the race: the OLD parent's fill arrives after the re-place -> successor cancelled, book = old ref
    qty = tr.brackets[ref1]["qty"]
    fill = NS(execution=NS(price=99.81, shares=qty), time="2026-08-20 09:55:00")
    tr._fill_logger("ENTRY")(trades[0], fill)
    assert cancelled[-1] == p["ref"] and "TEST" not in tr.pending
    assert tr.open_real[ref1]["qty"] == qty and any("filled while being re-placed" in l for l in said)
    # a bracket that already (partly) filled is never re-placed
    tr, trades, cancelled, said = _armed_trader()
    tr.reconcile("TEST", "1min", bars_from_hl(hl[:25]))
    trades[0].orderStatus.filled = 3
    tr.reconcile("TEST", "1min", bars_from_hl(hl[:26]))
    assert cancelled == [] and len(trades) == 3 and "TEST" in tr.pending, "no re-place after a fill"


def test_fill_logger_tracks_shares_per_ref():
    """ALMS 2026-09-02: TP filled 85 of 2,552 and the bot booked the trade CLOSED. The book
    must count shares: closed only at zero, exit price blended over the partial legs."""
    from types import SimpleNamespace as NS
    tr, trades, cancelled, said = _armed_trader()
    ref = "cuph-ALMS-15s-2026-09-02-rim40"
    tr.brackets[ref] = dict(tf="15s", stop=10.52, target=10.79, trigger=10.55)
    c = NS(symbol="ALMS")
    def fill(kind, otype, level, sh, px):
        o = NS(orderRef=ref, orderType=otype, action="BUY" if kind == "ENTRY" else "SELL",
               lmtPrice=level if otype == "LMT" else 0, auxPrice=level if otype == "STP" else 0)
        tr._fill_logger(kind)(NS(contract=c, order=o), NS(execution=NS(price=px, shares=sh), time="t"))
    fill("ENTRY", "STP", 10.55, 2552, 10.56)
    assert tr.open_real[ref]["qty"] == 2552 and abs(tr.open_real[ref]["entry"] - 10.56) < 1e-9
    fill("TP", "LMT", 10.79, 85, 10.79)
    assert ref in tr.open_real and tr.open_real[ref]["qty"] == 2467, "a partial exit keeps the rest on the book"
    assert tr.closed == [] and any("still on the book" in l for l in said)
    fill("SL", "STP", 10.52, 2467, 10.51)
    assert ref not in tr.open_real and len(tr.closed) == 1
    blended = (85 * 10.79 + 2467 * 10.51) / 2552
    assert abs(tr.closed[0]["exit"] - blended) < 1e-9 and tr.closed[0]["kind"] == "SL"


def test_flatten_closes_own_book_only_via_smart_and_verifies():
    """2026-09-01: DUOL survived overnight (close placed on the LISTING exchange, rejected
    silently). 2026-09-02: reqGlobalCancel + 'close every account position' also hit the
    tight-flag robot's orders and shares. The flatten must cancel only MY orders, close
    only MY book (one market order per ref, routed SMART), shout about account positions
    it does not own, and report what IBKR confirmed."""
    from types import SimpleNamespace as NS
    tr, trades, cancelled, said = _armed_trader()
    ref = "cuph-DUOL-15s-2026-09-01-rim7"
    tr.contracts = {"DUOL": NS(symbol="DUOL", exchange="SMART", conId=1)}
    tr.brackets = {ref: {}}
    tr.open_real = {ref: dict(sym="DUOL", tf="15s", entry=157.97, qty=247, cost=247 * 157.97, out=0.0,
                              out_qty=0, stop=157.50, target=160.0, trigger=157.97, ts=None)}
    tr.pending = {}
    book = [NS(contract=NS(symbol="DUOL"), orderStatus=NS(status="Submitted", filled=0),
               order=NS(orderRef=ref, action="SELL", orderType="STP", parentId=0, totalQuantity=247)),
            NS(contract=NS(symbol="QQQ"), orderStatus=NS(status="Submitted", filled=0),
               order=NS(orderRef="TF-QQQ-1", action="SELL", orderType="STP", parentId=0, totalQuantity=100))]
    placed, status = [], ["Filled"]

    class FakeIB:
        def accountValues(self): return []
        def openTrades(self): return book
        def positions(self):
            return [NS(contract=NS(symbol="DUOL", exchange="NASDAQ"), position=347),   # 100 not ours
                    NS(contract=NS(symbol="QQQ", exchange="NASDAQ"), position=100)]     # tight-flag's
        def cancelOrder(self, o): cancelled.append(o.orderRef)
        def placeOrder(self, c, o):
            placed.append((c, o))
            return NS(orderStatus=NS(status=status[0]), log=[], fillEvent=_Ev())
    tr.ib = FakeIB()
    tr.flatten("EOD 15:49")
    assert cancelled == [ref], f"only MY order may be cancelled, got {cancelled}"
    assert len(placed) == 1, "exactly one close, for MY 247 DUOL — never the tight-flag QQQ"
    contract, order = placed[0]
    assert contract.exchange == "SMART", f"close must be routed SMART, got {contract.exchange!r}"
    assert order.action == "SELL" and order.totalQuantity == 247 and order.orderRef == ref
    assert any("DUOL: account holds +347" in l and "NOT mine" in l for l in said), said
    assert not any("QQQ" in l for l in said), "a symbol we do not trade is not our business"
    tr.verify_flatten()                                             # no loop here: caller verifies
    assert any("✅ close DUOL +247" in l for l in said), said
    assert not any("STILL OPEN" in l for l in said)
    # a rejected close must be shouted
    status[0] = "Inactive"
    tr._last_flatten = 0
    tr.flatten("EOD 15:49")
    tr.verify_flatten()
    assert any("STILL OPEN" in l and "DUOL +247" in l for l in said), said
    # 2026-09-02 storm: 74 flattens in 30s oversold into a SHORT. Inside the in-flight
    # window a re-fired flatten must place NOTHING.
    n = len(placed)
    tr.flatten("EOD 15:49")
    assert len(placed) == n and any("already in flight" in l for l in said)


def test_guard_protects_own_book_only_and_counts_only_live_children():
    """The watchdog: every share on MY book has a working stop, oversized exits are cut,
    a flat book keeps no exit orders — sized off MY fills, never the account view (which
    also holds the tight-flag robot's shares), counting only children whose parent filled
    (a resting bracket's children protect nothing), repairing INSIDE the ref's OCA group."""
    from types import SimpleNamespace as NS
    tr, trades, cancelled, said = _armed_trader()
    ref = "cuph-ALMS-15s-2026-09-02-rim40"
    nref = "cuph-NVDA-15s-2026-09-02-rim9"
    tr.contracts = {s: NS(symbol=s) for s in ("ALMS", "NVDA", "QQQ")}
    tr.open_real = {ref: dict(sym="ALMS", qty=2467, stop=10.52)}
    tr.brackets = {ref: {}, nref: {}}
    tr._parent_trades = {11: NS(orderStatus=NS(filled=2467)), 21: NS(orderStatus=NS(filled=0))}
    def o(sym, ref_, otype, qty, pid):
        return NS(contract=NS(symbol=sym), orderStatus=NS(status="Submitted", filled=0),
                  order=NS(orderRef=ref_, action="SELL", orderType=otype, totalQuantity=qty, parentId=pid))
    book = [o("ALMS", ref, "LMT", 2552, 11),                        # oversized TP, no stop at all
            o("NVDA", nref, "STP", 300, 21), o("NVDA", nref, "LMT", 300, 21),   # RESTING bracket's children
            o("QQQ", "TF-QQQ-1", "STP", 100, 0)]                     # tight-flag's, not ours
    placed = []

    class FakeIB:
        def positions(self):
            return [NS(contract=NS(symbol="ALMS"), position=2467), NS(contract=NS(symbol="QQQ"), position=100)]
        def openTrades(self): return book
        def placeOrder(self, c, o_): placed.append(o_); return NS(orderStatus=NS(status="Submitted"), log=[], fillEvent=_Ev())
        def cancelOrder(self, o_): cancelled.append(o_.orderRef)
    tr.ib = FakeIB()
    tr.guard_brackets()
    assert len(placed) == 1 and placed[0].orderType == "STP" and placed[0].totalQuantity == 2467
    assert abs(placed[0].auxPrice - 10.52) < 1e-9
    assert placed[0].ocaGroup == ref and placed[0].orderRef == ref, "repair joins the bracket's OCA group"
    assert cancelled == [ref], f"only the oversized ALMS take-profit may be cancelled, got {cancelled}"
    assert sum("🚨" in l for l in said) == 2
    assert sum("QQQ: account shows +100 sh, my book 0" in l for l in said) == 1
    tr.guard_brackets()                                              # nothing new, no repeat noise
    assert sum("QQQ: account shows" in l for l in said) == 1
    # the book went flat (TP filled the rest) but a stop is still working -> orphan, cancelled
    tr.open_real = {}
    book[:] = [o("ALMS", ref, "STP", 2467, 0)]
    tr.guard_brackets()
    assert cancelled[-1] == ref and any("orphan" in l for l in said)


# --------------------------------------------------------------------------- runner
if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"✅ {name}")
        except AssertionError as e:
            failed += 1
            print(f"❌ {name}: {e}")
        except Exception:
            failed += 1
            print(f"💥 {name} crashed:\n{traceback.format_exc()}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    _sys.exit(1 if failed else 0)
