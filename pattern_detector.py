"""
pattern_detector.py — CUP COFFEE Stage 3: Cup-and-Handle Detector (v2 spec)
==========================================================================
Rebuilt to the user's exact rules:

CUP
  * left rim = any closed bar that is a local peak (high >= the bars on either side)
  * cup length 15..60 bars (left rim -> right rim)
  * NO ATR depth filter
  * right rim = a later peak that recovers to within 25% of the cup depth below the
    left rim:  left_high - 0.25*depth <= right_high <= left_high
  * RIM-LINE rule: no bar between the rims may poke above the straight line drawn
    from the left-rim high to the right-rim high (the cup stays clean under it)
  cup_depth = left_rim_high - lowest low between the rims

HANDLE  (left rim of the handle = right rim of the cup)
  * 4..60 bars long (counting the rim)
  * RATCHET only in the opening `ratchet`-bar window: the lip = the highest high of the
    handle's first few bars; AFTER that the lip is FIXED (it does NOT chase a drift up)
  * lip must stay within 25% of max(left-to-bottom, lip-to-bottom) of the LEFT rim
  * depth (lip - handle_low) <= 20% of (lip - cup_low)

ENTRY / STOP
  * entry = a BUY-STOP at lip + $0.01; fills on the FIRST retouch at the 4th bar or later
    (a pure sideways chop just keeps waiting until the handle exceeds 50 bars -> give up)
  * stop  = handle low ;  R = entry - stop

EXIT (for the events.jsonl label; research re-labels full-path)
  * stop hit / measured-move target hit / else flat at 3:49pm ET (240-bar cap)

Downstream field names are unchanged (breakout_idx = the entry bar) so the factor
library and miner keep working.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as Date, time as dtime
from typing import Optional

from data_layer import Bars, _SyntheticProvider, DataLayer
from research_data import session_end_idx

logger = logging.getLogger("cupcoffee.detector")

EXIT_CUTOFF = dtime(15, 49)     # flat by 3:49pm ET


@dataclass
class CupHandleEvent:
    symbol: str
    day: Date
    timeframe: str
    cup_left_idx: int
    cup_bottom_idx: int
    cup_right_idx: int
    cup_depth: float
    lip_diff_frac: float
    handle_num: int
    handle_low: float
    breakout_idx: int          # the ENTRY bar (retouch of handle rim + $0.01)
    entry_price: float
    stop_price: float
    target_price: float
    risk_R: float
    outcome: int
    exit_idx: int
    pnl_R: float
    mfe_R: float
    mfe_idx: int
    mae_R: float


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

def _lowest_low(b: Bars, lo: int, hi: int) -> int:
    best_i, best = lo, float("inf")
    for i in range(lo, hi):
        if b.l[i] < best:
            best, best_i = b.l[i], i
    return best_i

def _is_peak(b: Bars, i: int) -> bool:
    if i <= 0 or i >= len(b) - 1:
        return False
    return b.h[i] >= b.h[i - 1] and b.h[i] >= b.h[i + 1]

def _last_bar_by_cutoff(b: Bars) -> int:
    for i in range(len(b) - 1, -1, -1):
        if b.ts[i].time() <= EXIT_CUTOFF:
            return i
    return len(b) - 1


# ----------------------------------------------------------------------------
# Detector
# ----------------------------------------------------------------------------

class PatternDetector:
    def __init__(self, config: dict):
        p = config["pattern"]
        self.cup_min = p.get("cup_min_bars", 15)
        self.cup_max = p.get("cup_max_bars", 60)
        self.rim_recov = p.get("right_rim_recovery_frac", 0.25)
        self.h_min = p.get("handle_min_bars", 4)
        self.h_max = p.get("handle_max_bars", 50)
        self.ratchet = p.get("handle_ratchet_bars", 4)
        self.h_depth_frac = p.get("handle_max_depth_frac", 0.20)
        self.entry_off = p.get("entry_offset_dollars", 0.01)
        self.max_hold = config.get("labeling", {}).get("max_hold_bars", 240)

    # --- public ---
    def detect(self, b: Bars, symbol: str, day: Date) -> list[CupHandleEvent]:
        n = len(b)
        if n < self.cup_min + self.h_min + 2:
            return []
        events: list[CupHandleEvent] = []
        taken_entries: set[int] = set()
        for li in range(1, n - 1):
            if not _is_peak(b, li):
                continue
            cup = self._find_cup(b, li)
            if cup is None:
                continue
            bottom_idx, ri = cup
            cup_low = b.l[bottom_idx]
            cup_depth = b.h[li] - cup_low
            handle = self._find_handle(b, ri, cup_low, b.h[li])
            if handle is None:
                continue
            h_left_idx, handle_low, entry_idx = handle
            if entry_idx in taken_entries:
                continue
            end_idx = session_end_idx(b, entry_idx, self.max_hold)
            if end_idx is None or end_idx <= entry_idx:   # 11:00-13:00 no-trade, or no hold room
                continue
            rim = b.h[h_left_idx]
            entry = rim + self.entry_off
            stop = handle_low
            if entry <= stop:
                continue
            R = entry - stop
            target = entry + cup_depth            # measured move (research re-labels)
            outcome, exit_idx, pnl_R, mfe_R, mfe_idx, mae_R = \
                self._label(b, entry_idx, end_idx, entry, stop, target, R)
            events.append(CupHandleEvent(
                symbol=symbol, day=day, timeframe=b.timeframe,
                cup_left_idx=li, cup_bottom_idx=bottom_idx, cup_right_idx=ri,
                cup_depth=cup_depth,
                lip_diff_frac=abs(b.h[li] - b.h[ri]) / cup_depth if cup_depth else 0.0,
                handle_num=1, handle_low=handle_low, breakout_idx=entry_idx,
                entry_price=entry, stop_price=stop, target_price=target, risk_R=R,
                outcome=outcome, exit_idx=exit_idx, pnl_R=pnl_R,
                mfe_R=mfe_R, mfe_idx=mfe_idx, mae_R=mae_R,
            ))
            taken_entries.add(entry_idx)
        return events

    # --- cup: left rim li already a peak; find a valid right rim ---
    def _find_cup(self, b: Bars, li: int):
        left_high = b.h[li]
        hi_lim = min(len(b), li + self.cup_max + 1)
        bottom_low = float("inf")          # running lowest low over the cup interior (li, ri)
        bottom_idx = li
        for ri in range(li + 1, hi_lim):
            j = ri - 1                     # extend interior to include bar ri-1
            if j >= li + 1 and b.l[j] < bottom_low:
                bottom_low, bottom_idx = b.l[j], j
            if ri - li < self.cup_min:
                continue
            if not _is_peak(b, ri):
                continue
            right_high = b.h[ri]
            cup_depth = left_high - bottom_low
            if cup_depth <= 0:
                continue
            # right rim recovers to within 25% of cup depth, and does not exceed the left rim
            if right_high < left_high - self.rim_recov * cup_depth or right_high > left_high:
                continue
            # RIM-LINE: nothing between the rims pokes above the line joining them
            if self._obstructed(b, li, left_high, ri, right_high):
                continue
            return bottom_idx, ri
        return None

    @staticmethod
    def _obstructed(b: Bars, li: int, left_high: float, ri: int, right_high: float) -> bool:
        span = ri - li
        slope = (right_high - left_high) / span
        for k in range(li + 1, ri):
            line = left_high + slope * (k - li)
            if b.h[k] > line + 1e-9:
                return True
        return False

    # --- handle: lip ratchets only in the opening window, then the buy-stop waits ---
    def _find_handle(self, b: Bars, ri: int, cup_low: float, left_lip: float):
        n = len(b)
        # PHASE 1 — RATCHET (only within the first `ratchet` bars from the cup right rim):
        # a higher high lifts the handle lip. We enter on a break of the lip, so the lip is
        # the HIGHEST high of the handle's opening window; after it, the lip is FIXED (it does
        # NOT chase a slow drift up — that was the old bug that dragged entries to the top).
        lip_idx = ri
        for k in range(ri + 1, min(n, ri + self.ratchet)):
            if b.h[k] > b.h[lip_idx]:
                lip_idx = k
        lip = b.h[lip_idx]
        cup_depth_for_handle = lip - cup_low
        if cup_depth_for_handle <= 0:
            return None
        # RIM SYMMETRY: the lip must stay within 25% of the LARGER cup depth of the left rim
        # — rejects a lip that ratcheted far above the cup (entering the top of a run-up).
        if abs(lip - left_lip) >= self.rim_recov * max(left_lip - cup_low, lip - cup_low):
            return None
        max_handle_depth = self.h_depth_frac * cup_depth_for_handle
        trigger = lip + self.entry_off                  # buy-stop at the lip high + $0.01
        # PHASE 2 — the handle must be >= h_min bars (incl. the rim) and resolve within h_max
        # bars: track the dip; ENTER on the FIRST retouch of the lip at the 4th bar or later.
        # A pure sideways chop just keeps waiting until the handle exceeds h_max -> give up.
        earliest = max(lip_idx + 1, ri + self.h_min - 1)   # earliest entry = 4th bar incl. rim
        handle_low = float("inf")
        for k in range(lip_idx + 1, min(n, ri + self.h_max)):
            if k >= earliest and handle_low < float("inf") and b.h[k] >= trigger:
                return lip_idx, handle_low, k           # RETOUCH -> buy-stop fills, ENTER
            handle_low = min(handle_low, b.l[k])
            if lip - handle_low > max_handle_depth:      # handle deeper than 20% of cup -> give up
                return None
        return None                                      # never retouched within h_max bars

    # --- label: stop / target / flat at 3:49pm (240-bar cap) ---
    def _label(self, b: Bars, entry_idx: int, end_idx: int, entry: float, stop: float,
               target: float, R: float):
        end = end_idx + 1                                  # session deadline (11:00 morning / 15:50 afternoon)
        mfe = mae = 0.0
        mfe_idx = entry_idx
        for j in range(entry_idx + 1, end):
            fav = (b.h[j] - entry) / R
            adv = (entry - b.l[j]) / R
            if fav > mfe:
                mfe, mfe_idx = fav, j
            if adv > mae:
                mae = adv
            if b.l[j] <= stop:
                return -1, j, -1.0, mfe, mfe_idx, mae
            if b.h[j] >= target:
                return +1, j, (target - entry) / R, mfe, mfe_idx, mae
        final = (b.c[end - 1] - entry) / R if end > entry_idx + 1 else 0.0
        return 0, end - 1, final, mfe, mfe_idx, mae


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = {"data": {"timeframes": ["1min"], "max_gap_bars": 3, "min_bars": 60},
           "pattern": {"cup_min_bars": 15, "cup_max_bars": 60,
                       "right_rim_recovery_frac": 0.25, "handle_min_bars": 4,
                       "handle_max_bars": 60, "handle_ratchet_bars": 4,
                       "handle_max_depth_frac": 0.20, "entry_offset_dollars": 0.01},
           "labeling": {"max_hold_bars": 240}}
    series = DataLayer(cfg, _SyntheticProvider()).load("DELL", Date(2026, 5, 29))
    bars = series.bars["1min"]
    evs = PatternDetector(cfg).detect(bars, "DELL", Date(2026, 5, 29))
    print(f"bars={len(bars)}  detected {len(evs)} cup-and-handle(s)")
    for e in evs:
        v = {1: "WIN", -1: "LOSS", 0: "TIME"}[e.outcome]
        print(f"  cup[{e.cup_left_idx}->{e.cup_bottom_idx}->{e.cup_right_idx}] "
              f"depth={e.cup_depth:.2f} handle_low={e.handle_low:.2f} "
              f"entry@{e.breakout_idx} {e.entry_price:.2f} stop {e.stop_price:.2f} "
              f"[{v}] {e.pnl_R:+.2f}R")
