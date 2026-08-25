"""
test_detector.py — prove Stage 3 captures EVERY cup-and-handle situation
========================================================================
Builds controlled price paths for each scenario in the Cup-Coffee definition
— ones that SHOULD be detected and ones that MUST be rejected — runs the
detector, and checks the result against what we know is correct.

Each scenario states its expected event count and (where relevant) the
expected outcome labels, then we verify the detector agrees.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date as Date, datetime, time
from typing import Optional

from data_layer import Bars
from pattern_detector import PatternDetector, atr

DAY = Date(2026, 5, 29)
LABEL = {1: "WIN", -1: "LOSS", 0: "TIME"}


# ---- price-path builder from linear segments + light noise ----
def build(segments: list[tuple[int, float, float]], seed: int = 1, noise: float = 0.04) -> Bars:
    prices: list[float] = []
    for (n, p0, p1) in segments:
        for j in range(n):
            f = j / (n - 1) if n > 1 else 0.0
            prices.append(p0 + (p1 - p0) * f)
    rnd = random.Random(seed)
    o=[]; h=[]; l=[]; c=[]; v=[]; ts=[]
    t0 = datetime.combine(DAY, time(9, 30))
    for px in prices:
        op = px + rnd.uniform(-noise, noise)
        cl = px + rnd.uniform(-noise, noise)
        hi = max(op, cl) + abs(rnd.uniform(0, noise))
        lo = min(op, cl) - abs(rnd.uniform(0, noise))
        o.append(round(op,3)); h.append(round(hi,3)); l.append(round(lo,3))
        c.append(round(cl,3)); v.append(10000); ts.append(t0)
    return Bars("TEST", DAY, "1min", ts, o, h, l, c, v, True)


# ---- reusable building blocks ----
LIP, BOT, DEPTH = 100.0, 94.0, 6.0          # cup: left lip 100, bottom 94, depth 6

def cup(right_lip: float, dur_leg: int = 25) -> list[tuple[int, float, float]]:
    return [(10, 96.0, LIP), (dur_leg, LIP, BOT), (dur_leg, BOT, right_lip)]

def handle(start: float, depth: float, length: int, breakout_to: float, rise: int = 12):
    return [(length, start, start - depth), (rise, start - depth, breakout_to)]


# ---- scenario registry ----
@dataclass
class Scenario:
    name: str
    bars: Bars
    expect_n: int
    expect_outcomes: Optional[list[int]] = None
    cfg_override: Optional[dict] = None


def base_cfg() -> dict:
    return {
        "pattern": {
            "cup_min_bars": 15, "cup_max_bars": 200,
            "cup_depth_min_atr": 1.0, "cup_depth_max_atr": 80.0,   # geometry isolated from depth gate
            "lip_diff_max_frac_of_depth": 0.25,
            "handle_min_bars": 4, "handle_max_depth_frac": 0.25,
            "breakout_buffer_atr": 0.1, "max_handles_per_cup": 3,
        },
        "labeling": {"max_hold_bars": 40},
    }


def scenarios() -> list[Scenario]:
    S = []

    # 1) classic symmetric cup + winning handle
    S.append(Scenario("symmetric cup + WIN handle",
        build(cup(100.0) + handle(100.0, 1.0, 8, 108.0)), 1, [1]))

    # 2) HIGHER right lip, within 25% of depth (1.2/6 = 20%) -> valid
    S.append(Scenario("higher right lip (within 25%)",
        build(cup(101.2) + handle(101.2, 1.0, 8, 109.0)), 1, [1]))

    # 3) LOWER right lip, within 25% (1.2/6 = 20%) -> valid
    S.append(Scenario("lower right lip (within 25%)",
        build(cup(98.8) + handle(100.0, 1.0, 8, 108.0)), 1, [1]))

    # 4) right lip TOO HIGH (1.8/6 = 30%) -> NOT a cup
    S.append(Scenario("right lip too high (>25%) -> reject",
        build(cup(101.8) + handle(101.8, 1.0, 8, 109.0)), 0))

    # 5) right lip TOO LOW (1.8/6 = 30%) -> NOT a cup
    S.append(Scenario("right lip too low (>25%) -> reject",
        build(cup(98.2) + handle(98.2, 1.0, 8, 106.0)), 0))

    # 6) handle TOO DEEP (1.8 > 0.25*6=1.5) -> no valid handle
    S.append(Scenario("handle too deep -> no event",
        build(cup(100.0) + handle(100.0, 1.8, 8, 108.0)), 0))

    # 7) handle TOO SHORT (2 < 4 bars) -> no valid handle
    S.append(Scenario("handle too short -> no event",
        build(cup(100.0) + handle(100.0, 1.0, 2, 108.0)), 0))

    # 8) NO breakout (never clears the lip) -> no event
    S.append(Scenario("no breakout -> no event",
        build(cup(100.0) + handle(100.0, 1.0, 8, 99.5)), 0))

    # 9) cup TOO SHORT (<15 bars) -> reject
    S.append(Scenario("cup too short -> reject",
        build([(3, 96, 100), (5, 100, 94), (5, 94, 100)] + handle(100.0, 1.0, 8, 108.0)), 0))

    # 10) handle that LOSES (breakout then reverses below stop)
    S.append(Scenario("handle LOSS",
        build(cup(100.0) + [(8, 100, 99), (5, 99, 100.6), (10, 100.6, 97.5)]), 1, [-1]))

    # 11) handle that TIMES OUT (breakout then drifts flat past max_hold)
    S.append(Scenario("handle TIMEOUT",
        build(cup(100.0) + [(8, 100, 99), (6, 99, 100.8), (45, 100.8, 101.2)]), 1, [0]))

    # 12) MULTIPLE handles: handle1 loses, higher lip forms, handle2 wins  (RULE 2)
    seg = (cup(100.0)
           + [(8, 100, 99), (5, 99, 100.6), (8, 100.6, 98.5)]    # handle1 -> stop hit (loss)
           + [(10, 98.5, 101.0)]                                  # NEW higher lip at 101
           + [(6, 101.0, 100.0), (14, 100.0, 109.0)])             # handle2 -> win
    S.append(Scenario("multi-handle: LOSS then WIN (Rule 2)", build(seg), 2, [-1, 1]))

    # 13) flat noise, no pattern -> nothing
    S.append(Scenario("flat noise -> no event",
        build([(120, 100, 100.2)]), 0))

    # 14) DEPTH GATE: same valid cup, but config cap set below its depth -> reject
    deep = base_cfg(); deep["pattern"]["cup_depth_max_atr"] = 1.0     # absurdly tight
    S.append(Scenario("depth gate rejects too-deep cup",
        build(cup(100.0) + handle(100.0, 1.0, 8, 108.0)), 0, None, deep))

    return S


def run():
    passed = 0
    print(f"{'scenario':<42}{'exp':>4}{'got':>4}  {'outcomes':<16}{'result'}")
    print("-" * 86)
    for sc in scenarios():
        cfg = sc.cfg_override or base_cfg()
        evs = PatternDetector(cfg).detect(sc.bars, "TEST", DAY)
        got_n = len(evs)
        got_out = [e.outcome for e in evs]
        ok = (got_n == sc.expect_n)
        if ok and sc.expect_outcomes is not None:
            ok = (got_out == sc.expect_outcomes)
        passed += ok
        out_str = ",".join(LABEL[o] for o in got_out) if got_out else "-"
        print(f"{sc.name:<42}{sc.expect_n:>4}{got_n:>4}  {out_str:<16}{'PASS' if ok else 'FAIL'}")
    print("-" * 86)
    print(f"{passed}/{len(scenarios())} scenarios passed")


if __name__ == "__main__":
    run()
