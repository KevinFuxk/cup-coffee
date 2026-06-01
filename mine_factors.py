"""
mine_factors.py — Stage 4, for real: score every factor across 1R..5R
======================================================================
Pulls it all together on the labeled events:

  for each trade:  full-path relabel (exact realized R at every take-profit)
                   + compute the diverse point-in-time factor library
  then per take-profit k in {1,2,3,4,5}, for every factor:
                   IC (rank corr factor vs realized R), high/low-half win rates,
                   lift vs the REAL base rate at k, and a high-vs-low separation
                   test — with a Benjamini-Hochberg correction across the whole
                   grid so we don't fool ourselves with 130 simultaneous tests.

Honesty built in:
  * baseline is the MEASURED base rate at each take-profit (not a hard-coded 0.60)
  * noise control factors are scored too; if a noise factor "passes", the bar is
    too low — they are the canary
  * the assembled table is cached (mined_table.json) so re-scoring is instant

Output: the factor x take-profit grid, what survives multiple-testing, and the
actionable expectancy of acting on each survivor.
"""

from __future__ import annotations

import os
import sys
import json
import math
from datetime import date as Date

from research_data import ResearchData, label_full_path
from feature_library import FeatureLibrary
from factor_scorer import spearman_ic

TAKE_PROFITS = (1, 2, 3, 4, 5)
TABLE_PATH = "mined_table.json"
SCORES_PATH = "factor_scores.json"


# ---- stats ----
def _norm_cdf(x): return 0.5*(1+math.erf(x/math.sqrt(2)))

def two_prop_p(w1, n1, w2, n2):
    """Two-sided test: do the high and low halves have different win rates?"""
    if n1 == 0 or n2 == 0:
        return 1.0
    p1, p2 = w1/n1, w2/n2
    p = (w1+w2)/(n1+n2)
    se = math.sqrt(p*(1-p)*(1/n1+1/n2))
    if se == 0:
        return 1.0
    return 2*(1-_norm_cdf(abs((p1-p2)/se)))

def benjamini_hochberg(pairs, alpha=0.10):
    """pairs: list of (key, p). Returns set of keys significant at FDR=alpha."""
    m = len(pairs)
    if m == 0:
        return set()
    ordered = sorted(pairs, key=lambda kp: kp[1])
    sig = set()
    for i, (key, p) in enumerate(ordered, start=1):
        if p <= (i/m)*alpha:
            sig = set(k for k, _ in ordered[:i])
    return sig


# ---- assemble the table (features + full-path labels), cached ----
def build_table(rd, lib, events, rebuild=False):
    if os.path.exists(TABLE_PATH) and not rebuild:
        return json.load(open(TABLE_PATH))
    rows = []
    for i, ev in enumerate(events):
        b = rd.bars(ev["symbol"], Date.fromisoformat(ev["day"]), ev["timeframe"])
        if b is None:
            continue
        lab = label_full_path(b, min(ev["breakout_idx"], len(b)-1),
                              ev["entry_price"], ev["stop_price"], ev["risk_R"])
        feats = lib.compute(ev)
        rows.append({"symbol": ev["symbol"], "day": ev["day"], "tier": ev.get("size_tier"),
                     "realized_R": lab["realized_R"], "win": lab["win"],
                     "full_mfe_R": lab["full_mfe_R"], "factors": feats})
        if (i+1) % 50 == 0:
            print(f"  ...assembled {i+1}/{len(events)} (fetching+caching)")
    json.dump(rows, open(TABLE_PATH, "w"))
    return rows


# ---- score one factor at one take-profit ----
def score_factor_tp(rows, fname, k):
    xs, pnl, win = [], [], []
    kk = str(k)
    for r in rows:
        v = r["factors"].get(fname)
        if v is None:
            continue
        xs.append(v); pnl.append(r["realized_R"][kk]); win.append(r["win"][kk])
    n = len(xs)
    if n < 30 or len(set(xs)) < 5:               # too few, or ~constant -> unscoreable
        return None
    base = sum(win)/n
    ic = spearman_ic(xs, pnl)
    med = sorted(xs)[n//2]
    hi = [i for i in range(n) if xs[i] >= med]
    lo = [i for i in range(n) if xs[i] < med]
    wh = sum(win[i] for i in hi); wl = sum(win[i] for i in lo)
    wr_hi = wh/len(hi) if hi else 0.0
    wr_lo = wl/len(lo) if lo else 0.0
    exp_hi = sum(pnl[i] for i in hi)/len(hi) if hi else 0.0   # expectancy of the high-factor half
    exp_lo = sum(pnl[i] for i in lo)/len(lo) if lo else 0.0   # expectancy of the low-factor half
    p = two_prop_p(wh, len(hi), wl, len(lo))
    return {"factor": fname, "k": k, "n": n, "base": base, "ic": ic,
            "wr_lo": wr_lo, "wr_hi": wr_hi, "lift": wr_hi-base,
            "exp_hi": exp_hi, "exp_lo": exp_lo, "p": p}


def main(rebuild=False):
    rd = ResearchData(os.environ["POLYGON_API_KEY"])
    lib = FeatureLibrary(rd)
    events = [json.loads(l) for l in open("events.jsonl")]
    print(f"Assembling table for {len(events)} trades (cached after first run)...")
    rows = build_table(rd, lib, events, rebuild=rebuild)
    print(f"Table ready: {len(rows)} trades.\n")

    factor_names = list(rows[0]["factors"].keys())
    real_factors = [f for f in factor_names if not f.startswith("noise_")]
    noise_factors = [f for f in factor_names if f.startswith("noise_")]

    # base rate per take-profit (the honest benchmark, replaces 0.60)
    print("=== REAL base rate at each take-profit (this REPLACES the old 0.60) ===")
    for k in TAKE_PROFITS:
        kk = str(k); wr = sum(r["win"][kk] for r in rows)/len(rows)
        exp = sum(r["realized_R"][kk] for r in rows)/len(rows)
        print(f"   {k}R: base win {wr*100:4.0f}%   avg result {exp:+.3f}R")

    # score everything
    results = []
    for f in real_factors + noise_factors:
        for k in TAKE_PROFITS:
            s = score_factor_tp(rows, f, k)
            if s:
                results.append(s)

    # multiple-testing correction over REAL factor x TP tests only
    real_pairs = [((r["factor"], r["k"]), r["p"]) for r in results if not r["factor"].startswith("noise_")]
    sig = benjamini_hochberg(real_pairs, alpha=0.10)

    # ---- the grid, per take-profit ----
    for k in TAKE_PROFITS:
        block = sorted([r for r in results if r["k"] == k], key=lambda r: -abs(r["ic"]))
        print(f"\n=== Take-profit {k}R   (factors ranked by |IC|) ===")
        print(f"  {'factor':<28}{'cat':<13}{'n':>4}{'IC':>7}{'wr_lo':>7}{'wr_hi':>7}{'lift':>7}{'exp_hi':>8}{'p':>8}  sig")
        for r in block:
            star = "**" if (r["factor"], r["k"]) in sig else ("noise" if r["factor"].startswith("noise_") else "")
            print(f"  {r['factor']:<28}{lib.category_of(r['factor']):<13}{r['n']:>4}{r['ic']:>+7.3f}"
                  f"{r['wr_lo']:>7.2f}{r['wr_hi']:>7.2f}{r['lift']:>+7.2f}{r['exp_hi']:>+8.2f}{r['p']:>8.4f}  {star}")

    # ---- what survives ----
    print("\n" + "="*72)
    survivors = [r for r in results if (r["factor"], r["k"]) in sig]
    if survivors:
        print(f"SURVIVES multiple-testing (FDR 10%): {len(survivors)} factor x take-profit pairs")
        for r in sorted(survivors, key=lambda r: -abs(r["ic"])):
            better = "HIGH end" if r["exp_hi"] > r["exp_lo"] else "LOW end"
            print(f"   {r['factor']} @ {r['k']}R | IC {r['ic']:+.3f} | "
                  f"low-half {r['exp_lo']:+.2f}R (win {r['wr_lo']*100:.0f}%)  vs  "
                  f"high-half {r['exp_hi']:+.2f}R (win {r['wr_hi']*100:.0f}%)  ->  favor the {better}")
    else:
        print("SURVIVES multiple-testing (FDR 10%): NONE.")
        print("  -> On 402 trades nothing clears the bar. Expected with ~70 winners;")
        print("     the 4-year pile (~2x data) is the next test. Strongest raw signals below.")
        top = sorted([r for r in results if not r['factor'].startswith('noise_')], key=lambda r: r['p'])[:6]
        for r in top:
            print(f"   (raw, NOT corrected) {r['factor']} @ {r['k']}R  IC {r['ic']:+.3f}  p={r['p']:.4f}  "
                  f"top-half {r['exp_hi']:+.2f}R/trade")

    # noise canary
    noise_hits = [r for r in results if r["factor"].startswith("noise_") and r["p"] < 0.05]
    print(f"\nnoise-control canary: {len(noise_hits)} of {len(noise_factors)*len(TAKE_PROFITS)} "
          f"noise tests had raw p<0.05 (expect ~{0.05*len(noise_factors)*len(TAKE_PROFITS):.0f} by chance).")

    json.dump(results, open(SCORES_PATH, "w"))
    print(f"\nScores saved to {SCORES_PATH} (becomes cycle 1 of the factor track record).")


if __name__ == "__main__":
    main(rebuild="--rebuild" in sys.argv)
