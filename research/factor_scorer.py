"""
factor_scorer.py — CUP COFFEE Stage 4: Factor Scoring Engine
============================================================
Takes the labeled events (Stage 3) plus a factor value attached to each, and
measures whether that factor actually separates winners from losers.

For each factor it reports:
  * IC        — rank correlation between the factor and the trade's P/L (in R)
  * win rates — win rate in the LOW half vs the HIGH half of the factor
  * lift      — high-half win rate minus the 0.60 baseline
  * p-value   — is that win rate really different from 0.60, or just noise?
  * verdict   — PASS only if enough events AND significant AND lifts the right way

It scores POOLED and PER SIZE-TIER, because an edge can live in giants but not
small-caps (the conditioning idea).

This file proves itself: the demo plants ONE real factor (stronger in mega-caps)
plus TWO pure-noise factors, then shows the scorer finds the real one and
REJECTS the fakes. A scorer fooled by noise would be dangerous.
"""

from __future__ import annotations
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)

import math
import random
from dataclasses import dataclass
from typing import Optional

# for the real feature-computation demo (chained from Stage 2/3)
from data_layer import Bars, _SyntheticProvider, DataLayer
from pattern_detector import PatternDetector, CupHandleEvent
from datetime import date as Date


# ----------------------------------------------------------------------------
# Math helpers (no external deps)
# ----------------------------------------------------------------------------

def _rank(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    for r, i in enumerate(order):
        ranks[i] = float(r)
    return ranks

def _pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 2:
        return 0.0
    ma, mb = sum(a)/n, sum(b)/n
    cov = sum((a[i]-ma)*(b[i]-mb) for i in range(n))
    va = sum((x-ma)**2 for x in a)
    vb = sum((y-mb)**2 for y in b)
    denom = math.sqrt(va*vb)
    return cov/denom if denom else 0.0

def spearman_ic(factor: list[float], pnl: list[float]) -> float:
    return _pearson(_rank(factor), _rank(pnl))

def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def prop_pvalue(wins: int, n: int, p0: float = 0.60) -> float:
    """Two-sided test: is the win rate different from p0? (normal approximation)"""
    if n == 0:
        return 1.0
    phat = wins / n
    se = math.sqrt(p0*(1-p0)/n)
    if se == 0:
        return 1.0
    z = (phat - p0) / se
    return 2 * (1 - _norm_cdf(abs(z)))


# ----------------------------------------------------------------------------
# Real feature computation — how a factor is pulled from a detected event
# ----------------------------------------------------------------------------

def compute_features(ev: CupHandleEvent, b: Bars) -> dict:
    """The three known-good Tier-1 factors, from stored event fields + bars."""
    bi = ev.breakout_idx
    prior = b.v[max(0, bi-20):bi] or [1]
    breakout_volume_ratio = b.v[bi] / (sum(prior)/len(prior))

    cup_v = b.v[ev.cup_left_idx:ev.cup_right_idx] or [1]
    handle_v = b.v[ev.cup_right_idx:bi] or [1]
    handle_volume_dryup = (sum(handle_v)/len(handle_v)) / (sum(cup_v)/len(cup_v))

    right_rim_recovery = b.h[ev.cup_right_idx] / b.h[ev.cup_left_idx]

    return {
        "breakout_volume_ratio": breakout_volume_ratio,
        "handle_volume_dryup": handle_volume_dryup,
        "right_rim_recovery": right_rim_recovery,
    }


# ----------------------------------------------------------------------------
# The scorer
# ----------------------------------------------------------------------------

@dataclass
class FactorScore:
    name: str
    n: int
    ic: float
    winrate_low: float
    winrate_high: float
    lift_vs_baseline: float
    p_value: float
    verdict: str


class FactorScorer:
    def __init__(self, baseline: float = 0.60, min_events: int = 200, alpha: float = 0.05):
        self.baseline = baseline
        self.min_events = min_events
        self.alpha = alpha

    def score(self, factor_vals: list[float], wins: list[int], pnl: list[float],
              name: str) -> FactorScore:
        n = len(factor_vals)
        ic = spearman_ic(factor_vals, pnl)
        # split at the median of the factor
        med = sorted(factor_vals)[n//2] if n else 0.0
        hi_idx = [i for i in range(n) if factor_vals[i] >= med]
        lo_idx = [i for i in range(n) if factor_vals[i] < med]
        wr_hi = sum(wins[i] for i in hi_idx)/len(hi_idx) if hi_idx else 0.0
        wr_lo = sum(wins[i] for i in lo_idx)/len(lo_idx) if lo_idx else 0.0
        lift = wr_hi - self.baseline
        p = prop_pvalue(sum(wins[i] for i in hi_idx), len(hi_idx), self.baseline)

        ok = (n >= self.min_events) and (p < self.alpha) and (abs(lift) >= 0.03)
        verdict = "PASS" if ok else ("thin" if n < self.min_events else "REJECT")
        return FactorScore(name, n, ic, wr_lo, wr_hi, lift, p, verdict)

    def report(self, table: list[dict], factors: list[str], tier: Optional[str] = None):
        rows = [r for r in table if tier is None or r["tier"] == tier]
        scope = tier or "ALL"
        print(f"\n=== Factor scores [{scope}]  (n={len(rows)}, baseline={self.baseline:.2f}) ===")
        print(f"  {'factor':<24}{'n':>5}{'IC':>7}{'wr_lo':>7}{'wr_hi':>7}{'lift':>7}{'p':>8}  verdict")
        results = []
        for f in factors:
            vals = [r["factors"][f] for r in rows]
            wins = [r["win"] for r in rows]
            pnl  = [r["pnl_R"] for r in rows]
            s = self.score(vals, wins, pnl, f)
            results.append(s)
        for s in sorted(results, key=lambda x: -abs(x.ic)):
            print(f"  {s.name:<24}{s.n:>5}{s.ic:>+7.3f}{s.winrate_low:>7.2f}"
                  f"{s.winrate_high:>7.2f}{s.lift_vs_baseline:>+7.2f}{s.p_value:>8.4f}  {s.verdict}")
        return results


# ----------------------------------------------------------------------------
# Demo
# ----------------------------------------------------------------------------

def _demo_features_from_real_events():
    cfg = {
        "data": {"timeframes": ["1min"], "max_gap_bars": 3, "min_bars": 60},
        "pattern": {"cup_min_bars": 15, "cup_max_bars": 120,
                    "cup_depth_min_atr": 1.0, "cup_depth_max_atr": 40.0,
                    "lip_diff_max_frac_of_depth": 0.25, "handle_min_bars": 4,
                    "handle_max_depth_frac": 0.25, "breakout_buffer_atr": 0.1,
                    "max_handles_per_cup": 3},
        "labeling": {"max_hold_bars": 240},
    }
    series = DataLayer(cfg, _SyntheticProvider()).load("DELL", Date(2026, 5, 29))
    bars = series.bars["1min"]
    events = PatternDetector(cfg).detect(bars, "DELL", Date(2026, 5, 29))
    print("=== Real features computed on detected events (wiring check) ===")
    for e in events:
        f = compute_features(e, bars)
        print(f"  handle#{e.handle_num} [{ {1:'WIN',-1:'LOSS',0:'TIME'}[e.outcome] }]  "
              f"vol_ratio={f['breakout_volume_ratio']:.2f}  "
              f"dryup={f['handle_volume_dryup']:.2f}  "
              f"rim_recovery={f['right_rim_recovery']:.3f}")


def _demo_scoring_proof():
    """Plant one REAL factor (stronger in mega) + two NOISE factors; confirm the
    scorer finds the real one and rejects the fakes."""
    rnd = random.Random(42)
    BASE = 0.60
    tier_mult = {"mega": 2.0, "large": 1.0, "small": 0.3}
    table = []
    for _ in range(600):
        tier = rnd.choice(["mega", "large", "small"])
        f_real = rnd.random()                 # the genuine factor (0..1)
        f_noise1 = rnd.random()               # pure noise
        f_noise2 = rnd.random()               # pure noise
        # win probability bends with f_real, scaled by tier
        p_win = BASE + 0.35 * (f_real - 0.5) * tier_mult[tier]
        p_win = min(0.97, max(0.05, p_win))
        win = 1 if rnd.random() < p_win else 0
        if win:
            pnl = rnd.uniform(0.8, 3.5) * (0.6 + f_real)
        else:
            pnl = -1.0 if rnd.random() < 0.7 else rnd.uniform(-0.6, 0.2)  # loss or timeout
        table.append({"tier": tier,
                      "factors": {"planted_real": f_real,
                                  "noise_A": f_noise1,
                                  "noise_B": f_noise2},
                      "win": win, "pnl_R": pnl})

    scorer = FactorScorer(baseline=BASE, min_events=200, alpha=0.05)
    factors = ["planted_real", "noise_A", "noise_B"]
    scorer.report(table, factors)                       # pooled
    scorer.report(table, factors, tier="mega")          # conditioning: effect should be strongest here
    scorer.report(table, factors, tier="small")         # and weakest here


if __name__ == "__main__":
    _demo_features_from_real_events()
    print()
    _demo_scoring_proof()
