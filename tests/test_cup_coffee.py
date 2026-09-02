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
        def reqGlobalCancel(self): pass
        def positions(self): return []
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


def test_flatten_routes_via_smart_and_verifies():
    """2026-09-01: DUOL survived overnight because the close was placed on the
    position's LISTING exchange contract (rejected silently). The flatten must route
    every close via SMART and report what IBKR confirmed."""
    from types import SimpleNamespace as NS
    tr = _mini_trader(replay=False)
    placed = []

    class FakeIB:
        def reqGlobalCancel(self): pass
        def accountValues(self): return []
        def sleep(self, s): pass
        def positions(self):
            return [NS(contract=NS(symbol="DUOL", exchange="NASDAQ", conId=1), position=247)]
        def qualifyContracts(self, c): return [c]
        def placeOrder(self, contract, order):
            placed.append((contract, order))
            return NS(orderStatus=NS(status="Filled"), log=[])
    tr.ib = FakeIB()
    tr.MarketOrder = lambda act, qty: NS(action=act, totalQuantity=qty)
    tr.flatten("EOD 15:49")
    assert len(placed) == 1, "exactly one close order for one open position"
    contract, order = placed[0]
    assert contract.exchange == "SMART", f"close must be routed SMART, got {contract.exchange!r}"
    assert order.action == "SELL" and order.totalQuantity == 247


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
