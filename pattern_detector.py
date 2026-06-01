"""
pattern_detector.py — CUP COFFEE Stage 3: Cup-and-Handle Detector + Labeler
===========================================================================
Scans a clean intraday series (from Stage 2) for cup-and-handle setups and
labels each one win / loss / timeout. The labeled events are the training set
that Stages 4-5 mine for factors.

Cup-Coffee rules encoded here:
  * cup >= 15 bars, depth in ATR units (auto-scales by name volatility)
  * LIP ASYMMETRY: |left_lip - right_lip| <= 25% of cup depth, else not a cup
  * handle >= 4 bars, depth <= 1/4 of cup depth (the 4:1 .. 5:1 ratio)
  * breakout = close above the lip + a small ATR buffer
  * MULTIPLE HANDLES: one cup can spawn several handles; after a stop-out a NEW
    HIGHER lip + fresh qualifying handle is a separate tradeable event
  * stop = handle low ; target = measured move (cup depth projected up)
  * triple-barrier label: +1 target first, -1 stop first, 0 timeout
    (also records MFE / its timing for the exit-timing research)

Runs as-is: `python pattern_detector.py` loads the Stage 2 synthetic day
(which contains a cup-and-handle) and prints the detected, labeled events.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from data_layer import Bars, _SyntheticProvider, DataLayer
from datetime import date as Date

logger = logging.getLogger("cupcoffee.detector")


# ----------------------------------------------------------------------------
# Output record — one labeled setup
# ----------------------------------------------------------------------------

@dataclass
class CupHandleEvent:
    symbol: str
    day: Date
    timeframe: str
    # cup geometry
    cup_left_idx: int
    cup_bottom_idx: int
    cup_right_idx: int
    cup_depth: float
    lip_diff_frac: float          # |L-R| / depth  (must be <= 0.25)
    # handle / trade
    handle_num: int               # 1, 2, 3 ... within this cup
    handle_low: float
    breakout_idx: int
    entry_price: float
    stop_price: float
    target_price: float
    risk_R: float
    # outcome
    outcome: int                  # +1 win / -1 loss / 0 timeout
    exit_idx: int
    pnl_R: float
    mfe_R: float                  # max favourable excursion (R)
    mfe_idx: int                  # when MFE happened (powers exit-timing research)
    mae_R: float


# ----------------------------------------------------------------------------
# Small numeric helpers
# ----------------------------------------------------------------------------

def atr(b: Bars) -> float:
    """Day-level ATR yardstick = mean true range across the session."""
    trs = []
    for i in range(1, len(b)):
        trs.append(max(b.h[i] - b.l[i],
                       abs(b.h[i] - b.c[i-1]),
                       abs(b.l[i] - b.c[i-1])))
    return sum(trs) / max(1, len(trs))

def swing_highs(b: Bars, k: int) -> list[int]:
    return [i for i in range(k, len(b) - k)
            if b.h[i] == max(b.h[i-k:i+k+1])]

def swing_lows_window(b: Bars, lo: int, hi: int) -> int:
    """Index of the lowest low in [lo, hi)."""
    best, best_i = float("inf"), lo
    for i in range(lo, hi):
        if b.l[i] < best:
            best, best_i = b.l[i], i
    return best_i


# ----------------------------------------------------------------------------
# Detector
# ----------------------------------------------------------------------------

class PatternDetector:
    def __init__(self, config: dict):
        self.p = config["pattern"]
        self.lab = config["labeling"]

    def detect(self, b: Bars, symbol: str, day: Date) -> list[CupHandleEvent]:
        a = atr(b)
        if a <= 0:
            return []
        events: list[CupHandleEvent] = []
        used_right = set()
        for cup in self._find_cups(b, a):
            if cup["right_idx"] in used_right:
                continue
            used_right.add(cup["right_idx"])
            events.extend(self._handles_for_cup(b, cup, a, symbol, day))
        return events

    # --- cup detection ---
    def _find_cups(self, b: Bars, a: float) -> list[dict]:
        cups = []
        highs = swing_highs(b, k=3)
        depth_lo = self.p["cup_depth_min_atr"] * a
        depth_hi = self.p["cup_depth_max_atr"] * a
        lip_cap = self.p.get("lip_diff_max_frac_of_depth", 0.25)
        for li in highs:
            left_lip = b.h[li]
            win_hi = min(len(b), li + self.p["cup_max_bars"])
            if win_hi - li < self.p["cup_min_bars"]:
                continue
            bottom_i = swing_lows_window(b, li + 1, win_hi)
            depth = left_lip - b.l[bottom_i]
            if not (depth_lo <= depth <= depth_hi):
                continue
            # right lip: first swing high after bottom that recovered near the lip
            for ri in highs:
                if ri <= bottom_i:
                    continue
                if ri - li < self.p["cup_min_bars"] or ri - li > self.p["cup_max_bars"]:
                    continue
                right_lip = b.h[ri]
                # recovered at least halfway back up?
                if right_lip < b.l[bottom_i] + 0.5 * depth:
                    continue
                lip_diff_frac = abs(left_lip - right_lip) / depth
                if lip_diff_frac > lip_cap:        # RULE 1: lip asymmetry cap
                    continue
                cups.append({
                    "left_idx": li, "bottom_idx": bottom_i, "right_idx": ri,
                    "depth": depth, "lip_diff_frac": lip_diff_frac,
                    "lip_level": max(left_lip, right_lip),
                })
                break
        return cups

    # --- handles (multiple) for one cup ---
    def _handles_for_cup(self, b: Bars, cup: dict, a: float,
                         symbol: str, day: Date) -> list[CupHandleEvent]:
        out: list[CupHandleEvent] = []
        resistance = cup["lip_level"]
        cup_depth = cup["depth"]
        i = cup["right_idx"]
        max_handles = self.p.get("max_handles_per_cup", 3)
        for handle_num in range(1, max_handles + 1):
            found = self._find_handle(b, i, resistance, cup_depth, a)
            if found is None:
                break
            handle_low, breakout_idx = found
            entry = b.c[breakout_idx]
            stop = handle_low
            if entry <= stop:
                break
            R = entry - stop
            target = entry + cup_depth          # measured move
            outcome, exit_idx, pnl_R, mfe_R, mfe_idx, mae_R = \
                self._triple_barrier(b, breakout_idx, entry, stop, target, R)
            out.append(CupHandleEvent(
                symbol=symbol, day=day, timeframe=b.timeframe,
                cup_left_idx=cup["left_idx"], cup_bottom_idx=cup["bottom_idx"],
                cup_right_idx=cup["right_idx"], cup_depth=cup_depth,
                lip_diff_frac=cup["lip_diff_frac"], handle_num=handle_num,
                handle_low=handle_low, breakout_idx=breakout_idx,
                entry_price=entry, stop_price=stop, target_price=target, risk_R=R,
                outcome=outcome, exit_idx=exit_idx, pnl_R=pnl_R,
                mfe_R=mfe_R, mfe_idx=mfe_idx, mae_R=mae_R,
            ))
            # RULE 2: next handle needs a NEW HIGHER lip after this attempt resolves
            nxt = self._next_higher_lip(b, exit_idx, resistance)
            if nxt is None:
                break
            resistance, i = b.h[nxt], nxt
        return out

    def _find_handle(self, b: Bars, start: int, resistance: float,
                     cup_depth: float, a: float):
        """Find a >=handle_min_bars pullback that stays shallow, then breaks out."""
        max_depth = cup_depth * self.p["handle_max_depth_frac"]   # 4:1 .. 5:1
        buffer = self.p["breakout_buffer_atr"] * a
        min_bars = self.p["handle_min_bars"]
        handle_low = float("inf")
        length = 0
        started = False
        for j in range(start + 1, len(b)):
            if started and length >= min_bars and b.c[j] > resistance + buffer:
                return handle_low, j                      # breakout confirmed
            if b.h[j] < resistance:                        # inside the pullback
                started = True
                length += 1
                handle_low = min(handle_low, b.l[j])
                if resistance - handle_low > max_depth:    # handle too deep -> invalid
                    return None
        return None

    def _next_higher_lip(self, b: Bars, after: int, prev_lip: float) -> Optional[int]:
        for i in swing_highs(b, k=3):
            if i > after and b.h[i] > prev_lip:
                return i
        return None

    def _triple_barrier(self, b: Bars, entry_idx: int, entry: float,
                        stop: float, target: float, R: float):
        max_hold = self.lab["max_hold_bars"] if "max_hold_bars" in self.lab else 240
        end = min(len(b), entry_idx + max_hold + 1)
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
        final = (b.c[end-1] - entry) / R if end > entry_idx + 1 else 0.0
        return 0, end - 1, final, mfe, mfe_idx, mae


# ----------------------------------------------------------------------------
# Demo — chain Stage 2 -> Stage 3 on the synthetic day
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = {
        "data": {"timeframes": ["1min"], "session": "RTH",
                 "adjustment": "split_div", "max_gap_bars": 3, "min_bars": 60},
        "pattern": {
            "cup_min_bars": 15, "cup_max_bars": 120,
            "cup_depth_min_atr": 1.0, "cup_depth_max_atr": 40.0,   # wide for synthetic demo
            "lip_diff_max_frac_of_depth": 0.25,                    # RULE 1
            "handle_min_bars": 4, "handle_max_depth_frac": 0.25,   # 4:1 ratio
            "breakout_buffer_atr": 0.1, "max_handles_per_cup": 3,  # RULE 2
        },
        "labeling": {"max_hold_bars": 240},
    }

    series = DataLayer(cfg, _SyntheticProvider()).load("DELL", Date(2026, 5, 29))
    bars = series.bars["1min"]
    events = PatternDetector(cfg).detect(bars, "DELL", Date(2026, 5, 29))

    print(f"\nATR(day) = {atr(bars):.3f}   bars = {len(bars)}")
    print(f"Detected {len(events)} labeled event(s):\n")
    for e in events:
        verdict = {1: "WIN ", -1: "LOSS", 0: "TIME"}[e.outcome]
        print(f"  cup[{e.cup_left_idx}->{e.cup_bottom_idx}->{e.cup_right_idx}] "
              f"depth={e.cup_depth:.2f} lip_diff={e.lip_diff_frac:.0%}  "
              f"handle#{e.handle_num} breakout@{e.breakout_idx} "
              f"entry={e.entry_price:.2f} stop={e.stop_price:.2f}  "
              f"[{verdict}] pnl={e.pnl_R:+.2f}R  MFE={e.mfe_R:.2f}R@bar{e.mfe_idx}")
