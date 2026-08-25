"""
loop.py — CUP COFFEE Stage 5: The Research Loop
===============================================
Ties everything together and runs continuously:

  score every factor  ->  gate it  ->  record its score in the track record
  ->  promote passers to LIVE  ->  retire LIVE factors that DECAY
  ->  when something dies, ask Claude for new ideas  ->  repeat

The KEY long-run asset is the FACTOR TRACK RECORD: every factor's score is kept
every cycle, so you can (a) tell durable edges from lucky ones and (b) catch a
live factor going stale. That history is what the decay monitor runs on.

Claude touches the loop in exactly one place here: request_hypotheses().
Everything else is deterministic code — the math decides what's real.

Honest limit: a proposed factor's "computation" is text; it must be implemented
as a real feature function (your review step) before it can be scored. The loop
automates everything around that one human checkpoint.

Runs as-is: `python loop.py` runs two cycles on synthetic data where a real
factor DECAYS between cycle 1 and 2 — so you see the track record capture the
fall, the monitor retire it, and a new hypothesis get requested.
"""

from __future__ import annotations
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)

import random
from dataclasses import dataclass, field
from typing import Optional

from factor_scorer import FactorScorer, FactorScore


# ----------------------------------------------------------------------------
# The factor track record (the long-run history you asked to keep)
# ----------------------------------------------------------------------------

@dataclass
class FactorTrack:
    name: str
    status: str = "candidate"          # candidate | live | retired
    history: list[dict] = field(default_factory=list)   # one entry per cycle

    def record(self, cycle: int, s: FactorScore):
        self.history.append({"cycle": cycle, "ic": s.ic, "lift": s.lift_vs_baseline,
                             "p": s.p_value, "verdict": s.verdict})

    def avg_lift(self) -> float:
        passing = [h["lift"] for h in self.history if h["verdict"] != "thin"]
        return sum(passing)/len(passing) if passing else 0.0


class Registry:
    def __init__(self):
        self.tracks: dict[str, FactorTrack] = {}

    def get(self, name: str) -> FactorTrack:
        if name not in self.tracks:
            self.tracks[name] = FactorTrack(name)
        return self.tracks[name]

    def live(self) -> list[str]:
        return [n for n, t in self.tracks.items() if t.status == "live"]


# ----------------------------------------------------------------------------
# The loop
# ----------------------------------------------------------------------------

class FactorLoop:
    def __init__(self, scorer: FactorScorer, registry: Registry,
                 decay_min_lift: float = 0.04):
        self.scorer = scorer
        self.reg = registry
        self.decay_min_lift = decay_min_lift

    def run_cycle(self, cycle: int, table: list[dict], factors: list[str]) -> dict:
        promoted, retired = [], []
        print(f"\n========== CYCLE {cycle} (events={len(table)}) ==========")
        print(f"  {'factor':<22}{'IC':>7}{'lift':>7}{'p':>8}  {'verdict':<8}status")
        for f in factors:
            vals = [r["factors"][f] for r in table]
            wins = [r["win"] for r in table]
            pnl  = [r["pnl_R"] for r in table]
            s = self.scorer.score(vals, wins, pnl, f)
            track = self.reg.get(f)
            track.record(cycle, s)

            # promote a passing candidate to live
            if s.verdict == "PASS" and track.status == "candidate":
                track.status = "live"; promoted.append(f)
            # retire a live factor that has decayed
            elif track.status == "live" and self._decayed(track, s):
                track.status = "retired"; retired.append(f)

            print(f"  {f:<22}{s.ic:>+7.3f}{s.lift_vs_baseline:>+7.2f}"
                  f"{s.p_value:>8.4f}  {s.verdict:<8}{track.status}")

        if promoted: print(f"  -> promoted to LIVE: {promoted}")
        if retired:  print(f"  -> RETIRED (decayed): {retired}")
        return {"promoted": promoted, "retired": retired}

    def _decayed(self, track: FactorTrack, latest: FactorScore) -> bool:
        # decayed if it lost significance OR its lift fell below the floor / half its history
        if latest.p_value >= self.scorer.alpha:
            return True
        if latest.lift_vs_baseline < self.decay_min_lift:
            return True
        if latest.lift_vs_baseline < 0.5 * track.avg_lift():
            return True
        return False

    def request_hypotheses(self, n: int = 3) -> list[dict]:
        """Claude touchpoint. Uses the real API if available, else returns a
        mock proposal so the loop still demonstrates end to end."""
        live = self.reg.live()
        try:
            from claude_research import propose_factors, FactorRecord
            recs = []
            for name in live:
                h = self.reg.get(name).history[-1]
                recs.append(FactorRecord(name, "tier1_setup", h["ic"], h["lift"], 0.3))
            return propose_factors(recs, n=n, tier_focus="intratrade")
        except Exception:
            return [{
                "name": "vwap_reclaim_after_breakout",
                "tier": "intratrade",
                "hypothesis": "price reclaiming VWAP within 10 bars of breakout raises win rate",
                "economic_mechanism": "VWAP reclaim shows institutional buyers defending the breakout level",
                "computation": "1 if close>VWAP within 10 bars post-breakout else 0",
                "expected_direction": "higher_value_raises_winrate",
            }]


# ----------------------------------------------------------------------------
# Demo — a factor that decays between two cycles
# ----------------------------------------------------------------------------

def make_events(strength: float, seed: int, n: int = 400) -> list[dict]:
    """strength = how strongly `planted_real` drives wins this cycle."""
    rnd = random.Random(seed)
    BASE = 0.60
    table = []
    for _ in range(n):
        f_real = rnd.random()
        p_win = min(0.97, max(0.05, BASE + strength * (f_real - 0.5)))
        win = 1 if rnd.random() < p_win else 0
        pnl = rnd.uniform(0.8, 3.0)*(0.6+f_real) if win else (-1.0 if rnd.random()<0.7 else rnd.uniform(-0.6,0.2))
        table.append({"tier": "all",
                      "factors": {"planted_real": f_real, "noise_A": rnd.random()},
                      "win": win, "pnl_R": pnl})
    return table


if __name__ == "__main__":
    scorer = FactorScorer(baseline=0.60, min_events=200, alpha=0.05)
    loop = FactorLoop(scorer, Registry())
    factors = ["planted_real", "noise_A"]

    # cycle 1: factor is STRONG -> passes, promoted to live
    loop.run_cycle(1, make_events(strength=0.50, seed=1), factors)
    # cycle 2: factor has DECAYED (regime shifted) -> caught and retired
    res = loop.run_cycle(2, make_events(strength=0.04, seed=2), factors)

    # show the track record (the long-run history)
    print("\n========== FACTOR TRACK RECORD ==========")
    for name, t in loop.reg.tracks.items():
        trail = "  ".join(f"c{h['cycle']}:lift{h['lift']:+.2f}" for h in t.history)
        print(f"  {name:<22}[{t.status:<8}]  {trail}")

    # a factor was retired -> ask Claude for replacements (mock if no API key)
    if res["retired"]:
        print("\n========== NEW HYPOTHESES REQUESTED (decay triggered) ==========")
        for p in loop.request_hypotheses(n=1):
            print(f"  proposed: {p['name']} [{p['tier']}]")
            print(f"    why: {p['economic_mechanism']}")
            print(f"    compute: {p['computation']}")
