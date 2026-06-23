"""
enrich_real_price.py — tag every trade with its REAL (un-split-adjusted) price + cost
=====================================================================================
WHY. Polygon serves split-ADJUSTED prices. A stock that later did big REVERSE splits
(failing penny stocks do this to avoid delisting under $1) has its historical price scaled
UP — e.g. DBGI shows as $678,750 in mid-2021 when it really traded ~$6 (3 reverse splits:
100:1, 25:1, 50:1 -> x125,000). That breaks the COST model: a fixed ¢/share fee divided by
the inflated dollar-stop rounds to ~0, so these names look free to trade when in reality
(penny prices, cents-wide stops) they are the MOST expensive names in the book.

WHAT. Reconstruct each trade's real price from the split history:
    factor      = PRODUCT( split_from / split_to )  over splits AFTER the trade day
    real_price  = adjusted_price / factor
    real_risk$  = adjusted_risk$ / factor           (same scale-down; the % stop is unchanged)
and write data/realprice.json = { event_key: {real_price, real_risk, rsplits} } so the
dashboard can (a) screen out sub-$15 real-price penny stocks and (b) cost survivors honestly.

`rsplits` = total reverse splits this ticker has EVER done — a distress/quality flag shown
per trade (NOT used to filter, for now — just a variable to eyeball).

RUN: in your terminal (needs POLYGON_API_KEY from ~/.zshrc). Splits are cached under
cache/splits/, so re-runs are instant and resume if interrupted.
    python enrich_real_price.py
"""
from __future__ import annotations
import os, json
from research_data import ResearchData

EVENTS = "data/events.jsonl"
OUT    = "data/realprice.json"


def key(e):
    return f'{e["symbol"]}|{e["day"]}|{e["timeframe"]}|{e.get("breakout_idx")}|{e.get("handle_num")}'


def main():
    rd = ResearchData(os.environ["POLYGON_API_KEY"])
    events = [json.loads(l) for l in open(EVENTS) if l.strip()]

    # 1) pull (and cache) the split history for every distinct ticker
    syms = sorted({e["symbol"].split(":")[-1] for e in events})
    print(f"pulling splits for {len(syms)} tickers (cached -> re-runs are instant)...")
    split_map = {}
    for i, s in enumerate(syms, 1):
        split_map[s] = rd.splits(s)
        if i % 100 == 0 or i == len(syms):
            print(f"  {i}/{len(syms)}")

    # 2) per trade: recover the real price + real dollar-risk, count reverse splits
    out = {}
    for e in events:
        sym = e["symbol"].split(":")[-1]
        res = split_map.get(sym, [])
        day = e["day"]
        factor = 1.0
        for sp in res:                                   # splits AFTER the trade inflate the adjusted price
            if sp.get("execution_date", "") > day and sp.get("split_to"):
                factor *= sp["split_from"] / sp["split_to"]
        adj = e.get("entry_price") or 0.0
        real_price = adj / factor if factor else adj
        real_risk  = (e.get("risk_R") or 0.0) / factor if factor else (e.get("risk_R") or 0.0)
        rsplits = sum(1 for sp in res
                      if sp.get("split_to") and sp["split_to"] < sp.get("split_from", 0))
        out[key(e)] = {"real_price": round(real_price, 4),
                       "real_risk":  round(real_risk, 6),
                       "rsplits":    rsplits}

    json.dump(out, open(OUT, "w"))

    # 3) quick summary
    n = len(out)
    below15 = sum(1 for v in out.values() if v["real_price"] < 15)
    anyrev  = sum(1 for v in out.values() if v["rsplits"] > 0)
    inflated = sum(1 for e in events if (e.get("entry_price") or 0) /
                   (out[key(e)]["real_price"] or 1) > 1.5)
    print(f"\nWrote {OUT}: {n} trades")
    print(f"  real price < $15  (dropped by the screen): {below15}  ({below15/n*100:.0f}%)")
    print(f"  ticker ever reverse-split (flagged):        {anyrev}  ({anyrev/n*100:.0f}%)")
    print(f"  price was inflated >1.5x by later splits:   {inflated}  ({inflated/n*100:.0f}%)")


if __name__ == "__main__":
    main()
