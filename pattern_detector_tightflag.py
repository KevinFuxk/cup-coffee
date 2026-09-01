"""
pattern_detector_tightflag.py — HIGH/LOW TIGHT FLAG detector (SEPARATE strategy)
================================================================================
USER SPEC (2026-07-25, updated at the gate-(a) review the same day).
5-minute bars ONLY, at the open, LONG OR SHORT:

  BAR 1 = 09:30-09:35, BAR 2 = 09:35-09:40 (clock-aligned windows).
  SIDE (user decision at gate a): bar 1's COLOR picks the direction —
    green bar 1 (close > open)  ->  LONG  (the original rules)
    red   bar 1 (close < open)  ->  SHORT (every rule mirrored)
    doji  bar 1 (close = open)  ->  no trade (reject doji_bar1)

  ENTRY (USER 2026-07-28) — a resting STOP order placed the moment bar 2 closes:
    LONG  buy-stop  at max(bar1 high, bar2 high)   |  SHORT sell-stop at min(bar1 low, bar2 low)
    i.e. the extreme of the FIRST TWO BARS, whichever is further out. A touch is
    enough. It may ONLY trigger during BAR 3 (09:40-09:45); if bar 3 never reaches
    the level there is NO TRADE (reject "no_trigger"). A bar-3 open already through
    the level = gapped past the order -> fill at that open.
  The setup qualifies on TIGHTNESS alone (both sides): a 2:1 ratio between bar 1
  and bar 2 (range = high-to-low), i.e. bar1_range >= 2.0 x bar2_range. Perfect
  case: even more than 2:1.
    NOTE (USER 2026-08-06): the old overshoot cap — bar 2 may not sit past bar 1
    by more than 50% of bar 2's own range — is DELETED, not toggled. Setups like
    PLTR 2026-08-05 (bar 2 poking 0.95x its range above bar 1) now qualify. The
    geometry is still recorded as high_diff_frac / low_diff_frac for research.

  STOP:  LONG = the low of bar 2.   SHORT = the high of bar 2.
  R (the unit for ALL R math) = |entry - stop|  (USER 2026-07-28).
      bar 2's range is still recorded as range2 for research.
  NO take-profit. Exit ONLY by stop-out (or flat at 15:49 ET, the shared
  intraday session rule).
  FLY TRIGGER (USER 2026-07-25 complement): the trade becomes a "fly case" ONLY
  if its favorable excursion reaches +1.75R during the 3rd or 4th bar (by
  09:50). Fly case -> the trailing stop below is active. Any other case ->
  THE STOP NEVER MOVES from bar 2's low/high (stop-out or 15:49 flat only).
  TRAILING (fly cases only, mirrored per side): at the CLOSE of bar N (N >= 4,
  counting bar 1 at the open as 1):
    LONG:  if bar N is GREEN and makes a HIGHER HIGH  -> stop rises to the LOW
           of the previous printed bar (two behind the forming bar); never down.
    SHORT: if bar N is RED  and makes a LOWER LOW     -> stop drops to the HIGH
           of the previous printed bar; never up.
  Spec example reproduced (long): when bar 4 finishes and bar 5 starts forming,
  the stop moves from bar2's low to bar3's low (provided the fly armed).
  A threshold hit DURING bar 4 allows the move at bar 4's own close; if the
  bar touches the stop, the stop-first convention exits before any arming.

USER DECISIONS AT THE GATE-(a) REVIEW (2026-07-25):
  * Two-sided by bar-1 color (above) — replaces the always-long first build.
  * COVERAGE: both setup windows must contain ALL 5 of their minutes
    (min_coverage = 5). Kills the ghost-liquidity class: 44 thin-window events
    carried ~half the first pile's gross R with untradable one-print "flags".
  * skip_gap_past_stop: an entry whose 09:40 first print already opens at/past
    the stop (long: <= bar2 low; short: >= bar2 high) is SKIPPED, not logged
    as a 0R pseudo-trade.
  * Trailing stays per-bar ("it does not matter... as long as there is an
    upward trend"): one qualifying bar advances the stop; a non-qualifying bar
    pauses it (never retreats); the trail resumes while the trend continues.

INTERPRETATIONS CODED (presented at gate a; standing unless the user changes):
  * Tightness "at least the 50% ... 2:1 ratio ... perfect case even more than
    2:1" is coded as bar1 >= 2 x bar2 — bar 2 is the TIGHT flag (the literal
    "at least 50%" reading would contradict the stated perfect case).
  * Entry price = open of the first 1-minute bar at/after 09:40 (market order
    at 09:40:01); entry_delay_min records late first prints.
  * Intrabar convention matches the frozen labeler: if a bar spans the stop,
    the STOP fires (conservative); a gap through the stop exits at that bar's
    open. The stop active DURING bar N is the one set by the close of bar N-1
    (a trail move at N's own close cannot protect within N — no look-ahead).
  * The stop fires on ANY touch (long: low <= stop; short: high >= stop),
    whatever the bar's color — a resting stop order fills on a wick too; the
    spec's "red bar" is descriptive. "Breaks" is coded inclusively (<=/>=).
  * A zero-range bar 2 is REJECTED (zero_range2): R would be 0.
  * On thin names with EMPTY 5-min windows after entry, the trail and its
    higher-high / lower-low test use the previous PRINTED bar (the previous
    candle on the chart), not the empty clock slot; the resulting stop is at
    or behind the spec's — conservative. The 15:49 EOD flat (an addition to
    the spec's "exit only by stop-out"; never fired in 5.5yr) exits at the
    close of the LAST bar whose window STARTS at/before 15:49 (the 15:45-15:50
    bar), and exit_time reports the bar's start. Matches the cup harness.
  * mfe_R / day_high_R are FAVORABLE excursion in the trade's direction (for
    shorts: downward), measured up to & incl. the exit bar / to 15:49.

Fully separate from the cup-and-handle strategy: this file does not import from
or modify pattern_detector.py / live_trader_ibkr.py, and its events live in
data/events_tightflag.jsonl. Shared plumbing reused: data_layer.Bars and the
cached 1-minute bars (research_data.ResearchData).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import date as Date, time as dtime
from typing import Optional

from data_layer import Bars

logger = logging.getLogger("tightflag.detector")

CONFIG = {
    "strategy": "tightflag",
    "timeframe": "1min",           # USER DECISION 2026-09-02: HTF runs on 1-MINUTE bars
                                   #   (rules unchanged — only the clock width moved)
    "ratio_min": 2.0,             # bar1 range >= ratio_min x bar2 range (both sides)
    "min_coverage": 1,            # full coverage of the window (was 5 on 5-min bars;
                                  #   a 1-min window is fully covered by its 1 print)
    "fly_trigger_R": 1.75,        # USER 2026-07-25: trailing arms only if favorable
    "fly_by_bar": 4,              #   excursion hits fly_trigger_R during clock bars 3-4
    "trail_from_bar": 4,          # first stop move at the CLOSE of this bar (1-based)
    "trail_lag_bars": 2,          # stop -> prev printed bar's low/high (2 behind the forming bar)
    "trigger_bar": 2,             # USER 2026-07-28: the stop-entry may only fire during
                                  #   clock bucket 2 = BAR 3 (09:40-09:45). Never after.
    "eod_flat": "15:49",          # shared intraday session rule
    # USER CORRECTION 2026-07-28: R is the ENTRY-TO-STOP distance — the risk actually
    # taken, which depends on the quality of the fill. (The original spec said bar 2's
    # range; every earlier number in this project used that.) Kept switchable so the
    # two are comparable: "entry_stop" (LIVE) | "bar2_range" (legacy).
    #   entry_stop  -> R = |entry - stop|, so a clean stop-out is exactly -1.00R
    #   bar2_range  -> R = bar2 high - bar2 low
    "r_basis": "entry_stop",
    "skip_gap_past_stop": True,   # USER 2026-07-25: skip entries opening at/past the stop
    # USER 2026-07-27: a LONG is forbidden while the opening bar has not reclaimed
    # yesterday's close — if bar 1's HIGH is below prev_close, skip the long even on
    # a big green bar 1 (it is bouncing under resistance). SHORTS are unaffected:
    # they only ever fire on a red bar 1, which is allowed there.
    "long_needs_prev_close": True,
}

_OPEN_MIN = 9 * 60 + 30           # 09:30 in minutes-of-day
_ENTRY_T = dtime(9, 40)
_EOD_T = dtime(*map(int, CONFIG["eod_flat"].split(":")))


# ----------------------------------------------------------------------------
# clock-aligned 5-minute bars
# ----------------------------------------------------------------------------

def clock_bars(one: Bars, width: int) -> tuple[Bars, list[int], list[int]]:
    """Clock-aligned `width`-minute bars from 1-min bars: bucket k = [09:30+w*k, ...).
    Returns (bars, bucket_index per bar, 1-min coverage count per bar).

    This matches the live MinuteAggregator convention (windows anchored to the
    clock from 09:30). It differs from data_layer.downsample — which chunks by
    INDEX — only when 1-min bars are missing; for this strategy the first two
    windows ARE the pattern, so the windows must be clock-true.

    Buckets with no prints are skipped (no bar exists), so consecutive list
    entries can span a gap on thin names; `buckets` keeps the true clock slot."""
    o = []; h = []; l = []; c = []; v = []; ts = []
    buckets: list[int] = []
    cov: list[int] = []
    for i in range(len(one)):
        t = one.ts[i]
        mod = t.hour * 60 + t.minute
        k = (mod - _OPEN_MIN) // width
        if mod < _OPEN_MIN or k < 0:
            continue
        if buckets and buckets[-1] == k:
            h[-1] = max(h[-1], one.h[i]); l[-1] = min(l[-1], one.l[i])
            c[-1] = one.c[i]; v[-1] += one.v[i]; cov[-1] += 1
        else:
            start = _OPEN_MIN + width * k
            o.append(one.o[i]); h.append(one.h[i]); l.append(one.l[i])
            c.append(one.c[i]); v.append(one.v[i])
            ts.append(t.replace(hour=start // 60, minute=start % 60,
                                second=0, microsecond=0))
            buckets.append(k); cov.append(1)
    bars = Bars(one.symbol, one.date, f"{width}min", ts, o, h, l, c, v, one.adjusted)
    return bars, buckets, cov


# ----------------------------------------------------------------------------
# event contract
# ----------------------------------------------------------------------------

@dataclass
class TightFlagEvent:
    symbol: str
    day: Date
    timeframe: str                 # the clock width the event was built on
    side: str                      # "long" | "short" (bar 1's color)
    # the two setup bars (clock windows 09:30-09:35 and 09:35-09:40)
    b1_o: float; b1_h: float; b1_l: float; b1_c: float; b1_v: float
    b2_o: float; b2_h: float; b2_l: float; b2_c: float; b2_v: float
    cov1: int                      # 1-min bars present in window 1 (of 5)
    cov2: int                      # 1-min bars present in window 2 (of 5)
    range1: float
    range2: float
    ratio: float                   # range1 / range2  (>= ratio_min by rule)
    high_diff: float               # h2 - h1  (positive = bar2 poked above bar1)
    high_diff_frac: float          # (h2 - h1) / range2 — the long cap operand
    low_diff: float                # l1 - l2  (positive = bar2 poked below bar1)
    low_diff_frac: float           # (l1 - l2) / range2 — the short cap operand
    bar1_green: bool
    bar2_green: bool
    inside_bar: bool               # bar2 fully inside bar1's range
    # trade
    entry_time: str                # first 1-min print at/after 09:40 (ET)
    entry_delay_min: int           # minutes past 09:40 of that first print
    entry_price: float             # its open (market order at 09:40:01)
    stop_price: float              # long: bar2 low | short: bar2 high
    r_unit: float                  # the R denominator (see CONFIG['r_basis'])
    entry_stop_R: float            # |entry - stop| / r_unit — initial risk in R
    # outcome — trailing-stop replay to 15:49 (gross, before slippage/commission)
    exit_reason: str               # "stop" | "eod"
    exit_bar: int                  # 1-based clock bar number of the exit
    exit_time: str
    exit_price: float
    pnl_R: float                   # favorable-signed: positive = profit, either side
    mfe_R: float                   # best favorable excursion up to & incl. the exit bar
    mfe_bar: int
    mae_R: float                   # worst adverse excursion up to & incl. the exit bar
    day_high_R: float              # best favorable R to 15:49 IGNORING the exit —
    day_high_bar: int              #   "the highest Rs we reached" for research
    fly: bool                      # hit +1.75R during bars 3-4 -> trailing armed
    fly_bar: int                   # clock bar where it armed (0 = never; stop fixed)
    trail_moves: int               # how many times the trailing stop advanced
    final_stop: float
    trail_path: list               # [[clock bar N whose CLOSE moved it, new stop], ...]


def event_record(e: TightFlagEvent) -> dict:
    rec = asdict(e)
    rec["day"] = e.day.isoformat()
    return rec


# ----------------------------------------------------------------------------
# detection — uses ONLY bar 1 and bar 2 (no look-ahead by construction)
# ----------------------------------------------------------------------------

def detect(five: Bars, buckets: list[int], cov: list[int],
           cfg: dict = CONFIG) -> tuple[Optional[dict], str]:
    """Check the first two clock windows for the tight-flag setup (either side).
    Returns (setup dict incl. "side", "") on a pass, or (None, reject_reason)."""
    if len(five) < 2 or buckets[0] != 0:
        return None, "no_bar1"
    if buckets[1] != 1:
        return None, "no_bar2"
    if cov[0] < cfg["min_coverage"] or cov[1] < cfg["min_coverage"]:
        return None, "coverage"
    h1, l1 = five.h[0], five.l[0]
    h2, l2 = five.h[1], five.l[1]
    range1, range2 = h1 - l1, h2 - l2
    if range2 <= 0:
        return None, "zero_range2"
    # side = bar 1's color (USER 2026-07-25)
    if five.c[0] > five.o[0]:
        side = "long"
    elif five.c[0] < five.o[0]:
        side = "short"
    else:
        return None, "doji_bar1"
    if range1 < cfg["ratio_min"] * range2:
        return None, "ratio"
    # USER 2026-08-06: the overshoot cap (bar 2 may not poke past bar 1 by more than
    # 50% of its own range) is DELETED. Only the 2:1 tightness and the side rule gate a
    # setup now. high_diff_frac / low_diff_frac are still RECORDED per event so the
    # geometry stays available to research — they just no longer reject anything.
    # USER SPEC 2026-07-28 — ENTRY LEVEL: a resting STOP order at the extreme of the
    # first two bars, whichever is further out. LONG: max(bar1 high, bar2 high).
    # SHORT: min(bar1 low, bar2 low). It may only trigger during BAR 3 (09:40-09:45);
    # a touch is enough. If bar 3 never reaches it, there is no trade.
    entry_level = max(h1, h2) if side == "long" else min(l1, l2)
    return {
        "side": side, "entry_level": entry_level,
        "h1": h1, "l1": l1, "h2": h2, "l2": l2,
        "range1": range1, "range2": range2,
        "ratio": range1 / range2,
        "high_diff": h2 - h1, "high_diff_frac": (h2 - h1) / range2,
        "low_diff": l1 - l2, "low_diff_frac": (l1 - l2) / range2,
        "cov1": cov[0], "cov2": cov[1],
    }, ""


# ----------------------------------------------------------------------------
# trailing-stop replay (the label) — stop-out or flat at 15:49, no take-profit
# ----------------------------------------------------------------------------

def label_trail(five: Bars, buckets: list[int], entry_price: float,
                stop0: float, r_unit: float, side: str = "long",
                cfg: dict = CONFIG) -> dict:
    """Walk the 5-min bars from the entry bar (clock bar 3) to 15:49.
    The stop active DURING bar N is the one set by the close of bar N-1.
    FLY TRIGGER (USER 2026-07-25): trailing is armed only if the favorable
    excursion reaches fly_trigger_R (+1.75R) during clock bars 3-4; otherwise
    the stop never moves. Once armed, after a bar survives, at its close
    (clock bar >= trail_from_bar):
      LONG:  a GREEN bar with a HIGHER HIGH ratchets the stop UP to the LOW of
             the previous printed bar.
      SHORT: a RED bar with a LOWER LOW ratchets the stop DOWN to the HIGH of
             the previous printed bar.
    trail_lag_bars = 2 -> the reference bar is 2 behind the bar now forming."""
    lng = side == "long"
    idxs = [i for i in range(len(five))
            if buckets[i] >= 2 and five.ts[i].time() <= _EOD_T]
    stop = stop0
    trail_moves = 0
    trail_path: list[list] = []
    fly = False; fly_bar = 0
    mfe = 0.0; mfe_bar = 3
    mae = 0.0
    day_high = 0.0; day_high_bar = 3
    exit_i = None; exit_price = None; exit_reason = "eod"

    def fav_of(i):    # favorable excursion of bar i, in R, trade direction
        return ((five.h[i] - entry_price) if lng else (entry_price - five.l[i])) / r_unit

    def adv_of(i):    # adverse excursion of bar i, in R
        return ((entry_price - five.l[i]) if lng else (five.h[i] - entry_price)) / r_unit

    # full-day favorable path first (ignores the exit — pure research field)
    for i in idxs:
        f = fav_of(i)
        if f > day_high:
            day_high, day_high_bar = f, buckets[i] + 1

    lag = cfg["trail_lag_bars"] - 1   # printed bars back from the just-closed bar
    for n, i in enumerate(idxs):
        f, a = fav_of(i), adv_of(i)
        if f > mfe:
            mfe, mfe_bar = f, buckets[i] + 1
        if a > mae:
            mae = a
        hit = (five.l[i] <= stop) if lng else (five.h[i] >= stop)
        if hit:                                     # stop first (conservative)
            exit_i = i
            exit_price = (min(stop, five.o[i]) if lng else max(stop, five.o[i]))
            exit_reason = "stop"                    # gap through -> the open
            break
        # survived the bar -> arm the fly if +1.75R was reached in bars 3-4
        barnum = buckets[i] + 1                     # 1-based clock bar number
        if not fly and barnum <= cfg["fly_by_bar"] and f >= cfg["fly_trigger_R"]:
            fly, fly_bar = True, barnum
        # trailing update (fly cases ONLY) takes effect from the NEXT bar
        if fly and n >= lag and barnum >= cfg["trail_from_bar"]:
            prev = idxs[n - lag]
            if lng:
                ok = five.c[i] > five.o[i] and five.h[i] > five.h[prev]
                if ok and five.l[prev] > stop:
                    stop = five.l[prev]
                    trail_moves += 1
                    trail_path.append([barnum, stop])
            else:
                ok = five.c[i] < five.o[i] and five.l[i] < five.l[prev]
                if ok and five.h[prev] < stop:
                    stop = five.h[prev]
                    trail_moves += 1
                    trail_path.append([barnum, stop])

    if exit_i is None:                              # never stopped -> flat at EOD
        exit_i = idxs[-1]
        exit_price = five.c[exit_i]

    pnl = ((exit_price - entry_price) if lng else (entry_price - exit_price)) / r_unit
    return {
        "exit_reason": exit_reason,
        "exit_bar": buckets[exit_i] + 1,
        "exit_time": five.ts[exit_i].strftime("%H:%M"),
        "exit_price": exit_price,
        "pnl_R": pnl,
        "mfe_R": mfe, "mfe_bar": mfe_bar,
        "mae_R": mae,
        "day_high_R": day_high, "day_high_bar": day_high_bar,
        "fly": fly, "fly_bar": fly_bar,
        "trail_moves": trail_moves,
        "final_stop": stop,
        "trail_path": trail_path,
    }


# ----------------------------------------------------------------------------
# one (symbol, day) -> event or reject reason
# ----------------------------------------------------------------------------

def entry_fill(five: Bars, buckets: list[int], setup: dict,
               cfg: dict = CONFIG) -> Optional[tuple]:
    """Resting STOP-entry at setup['entry_level'], triggerable during BAR 3 only
    (clock bucket 2 = 09:40-09:45), a TOUCH being enough (USER 2026-07-28).

    Returns (fill_price, fill_ts, delay_min) or None if it never triggered.
    Fill convention (same as the cup harness): if bar 3 OPENS already through the
    level the market gapped past the order, so it fills at that open — worse than
    the level, which is the honest outcome. Otherwise it fills AT the level."""
    trigger_bar = cfg.get("trigger_bar", 2)            # clock bucket of bar 3
    i = next((j for j, k in enumerate(buckets) if k == trigger_bar), None)
    if i is None:
        return None                                    # bar 3 never printed
    lvl = setup["entry_level"]
    lng = setup["side"] == "long"
    o, h, l = five.o[i], five.h[i], five.l[i]
    if lng:
        if o >= lvl:
            px = o                                     # gapped through the buy-stop
        elif h >= lvl:
            px = lvl
        else:
            return None                                # never reached -> no trade
    else:
        if o <= lvl:
            px = o                                     # gapped through the sell-stop
        elif l <= lvl:
            px = lvl
        else:
            return None
    ts = five.ts[i]
    return px, ts, (ts.hour * 60 + ts.minute) - (9 * 60 + 40)


def r_unit_for(entry: float, stop: float, range2: float, cfg: dict = CONFIG) -> float:
    """The R denominator. USER 2026-07-28: R is the ENTRY-TO-STOP distance — the risk
    actually taken, so it moves with fill quality and a clean stop-out is exactly -1R.
    `bar2_range` reproduces every number produced before that correction."""
    if cfg.get("r_basis", "entry_stop") == "bar2_range":
        return range2
    return abs(entry - stop)


def prev_close_gate(setup: dict, prev_close: Optional[float],
                    cfg: dict = CONFIG) -> bool:
    """USER 2026-07-27. True = SKIP this setup.
    A LONG requires bar 1's high to have reclaimed yesterday's close; below it the
    market is bouncing under resistance, so the long is refused even on a big green
    bar 1. Shorts are untouched (they only fire on a red bar 1). No prev_close
    available (first cached day) -> no gate."""
    if not cfg.get("long_needs_prev_close") or prev_close is None:
        return False
    return setup["side"] == "long" and setup["h1"] < prev_close


def cfg_width(cfg: dict) -> int:
    """Bar width in minutes from the config timeframe ('1min' -> 1)."""
    return int(cfg["timeframe"].rstrip("min"))


def clock_5min(one: Bars) -> tuple[Bars, list[int], list[int]]:
    """Legacy alias — the OLD 5-min pile and its viewers were built on this width."""
    return clock_bars(one, 5)


def scan_day(one_min: Bars, symbol: str, day: Date,
             cfg: dict = CONFIG,
             prev_close: Optional[float] = None) -> tuple[Optional[TightFlagEvent], str]:
    five, buckets, cov = clock_bars(one_min, cfg_width(cfg))
    setup, why = detect(five, buckets, cov, cfg)
    if setup is None:
        return None, why
    if prev_close_gate(setup, prev_close, cfg):
        return None, "long_below_prev_close"
    side = setup["side"]
    lng = side == "long"
    stop0 = setup["l2"] if lng else setup["h2"]

    # USER SPEC 2026-07-28 — the resting stop-entry, triggerable during BAR 3 ONLY.
    fill = entry_fill(five, buckets, setup, cfg)
    if fill is None:
        return None, "no_trigger"
    entry_price, entry_ts, delay = fill

    r_unit = r_unit_for(entry_price, stop0, setup["range2"], cfg)
    if r_unit <= 0:
        return None, "zero_r_unit"
    lab = label_trail(five, buckets, entry_price, stop0, r_unit, side, cfg)

    e = TightFlagEvent(
        symbol=symbol, day=day, timeframe=cfg["timeframe"], side=side,
        b1_o=five.o[0], b1_h=five.h[0], b1_l=five.l[0], b1_c=five.c[0], b1_v=five.v[0],
        b2_o=five.o[1], b2_h=five.h[1], b2_l=five.l[1], b2_c=five.c[1], b2_v=five.v[1],
        cov1=setup["cov1"], cov2=setup["cov2"],
        range1=setup["range1"], range2=setup["range2"], ratio=setup["ratio"],
        high_diff=setup["high_diff"], high_diff_frac=setup["high_diff_frac"],
        low_diff=setup["low_diff"], low_diff_frac=setup["low_diff_frac"],
        bar1_green=five.c[0] > five.o[0], bar2_green=five.c[1] > five.o[1],
        inside_bar=(five.h[1] <= five.h[0] and five.l[1] >= five.l[0]),
        entry_time=entry_ts.strftime("%H:%M"), entry_delay_min=delay,
        entry_price=entry_price, stop_price=stop0, r_unit=r_unit,
        entry_stop_R=abs(entry_price - stop0) / r_unit,
        **lab,
    )
    return e, ""


if __name__ == "__main__":
    # smoke test on one cached day (no network when the day is cached)
    import os, sys
    from research_data import ResearchData
    rd = ResearchData(os.environ.get("POLYGON_API_KEY", ""))
    sym = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    day = Date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else Date(2021, 8, 2)
    one = rd.bars(sym, day, "1min")
    if one is None:
        print(f"no cached bars for {sym} {day}")
        sys.exit(1)
    ev, why = scan_day(one, sym, day)
    if ev is None:
        print(f"{sym} {day}: no setup ({why})")
    else:
        print(f"{sym} {day}: TIGHT FLAG {ev.side.upper()}  ratio={ev.ratio:.2f}  "
              f"entry {ev.entry_price:.2f} stop {ev.stop_price:.2f}  R=${ev.r_unit:.2f}  "
              f"[{ev.exit_reason} bar{ev.exit_bar}] {ev.pnl_R:+.2f}R  "
              f"mfe {ev.mfe_R:+.2f}R  trail x{ev.trail_moves}")
