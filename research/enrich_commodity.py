"""
enrich_commodity.py — flag each ticker as commodity-sector (SIC-based, precise)
==============================================================================
WHY SIC, not keywords. A company's SIC code is its OFFICIAL industry — the precise
"is it IN the commodity sector" signal. Keywording a free-text business description
over-excludes: Caterpillar's description is full of "mining"/"oil&gas" because it
SELLS equipment to miners, but it's an industrial, not a commodity stock. SIC gets
this right (CAT = 3531 machinery -> kept; XOM = 2911 petroleum -> excluded).

Writes data/commodity.json = { TICKER: true/false } so the dashboard can COMPARE
include-vs-exclude (return + risk), not silently drop them.

Run in your terminal (POLYGON_API_KEY from ~/.zshrc). details() is cached -> fast.
    python enrich_commodity.py
"""
from __future__ import annotations
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)
import os, json
from research_data import ResearchData

OUT = "data/commodity.json"
EVENTS = "data/events.jsonl"

# SIC ranges = commodity producers/miners/ag. Edit freely — each line is one veto-able rule.
COMMODITY_SIC_RANGES = [
    (100, 999),    # agriculture, forestry, fishing (ag commodities)
    (1000, 1499),  # MINING: metal (gold 1040 / silver / copper 1000), coal 1200s,
                   #         oil & gas extraction + field services 1300s, nonmetallic 1400s
    (2870, 2879),  # agricultural chemicals (fertilizer)
    (2911, 2911),  # petroleum refining (Exxon, Chevron)
    (2990, 2990),  # petroleum & coal products
    (3310, 3317),  # steel works / blast furnaces / rolling mills + steel pipe
    (3330, 3341),  # primary & secondary nonferrous smelting/refining (copper, aluminum)
    #  NOTE: we deliberately SKIP 3350-3357 (nonferrous wire/cable drawing) and the foundry
    #  codes — those are downstream FABRICATION and caught non-commodity names like
    #  Corning (GLW), Optical Cable (OCC), and Howmet aerospace (HWM).
]
# SIC mis-tags a handful of obvious commodity names -> patch by hand (auditable, zero false positives):
COMMODITY_MANUAL = {"GOLD"}   # Barrick (ticker GOLD) is SIC 5094 "wholesale jewelry"(!). Add oddballs here.
COMMODITY_NEVER  = set()      # force-KEEP overrides: tickers SIC wrongly flags that you want kept tradable.


def is_commodity(ticker: str, sic) -> bool:
    if ticker.upper() in COMMODITY_NEVER:
        return False
    if ticker.upper() in COMMODITY_MANUAL:
        return True
    try:
        s = int(sic)
    except (TypeError, ValueError):
        return False                                  # unknown SIC -> NOT excluded (avoid over-exclusion)
    return any(lo <= s <= hi for lo, hi in COMMODITY_SIC_RANGES)


def main():
    rd = ResearchData(os.environ["POLYGON_API_KEY"])
    syms = sorted({json.loads(l)["symbol"].split(":")[-1] for l in open(EVENTS) if l.strip()})
    out, flagged = {}, []
    for i, t in enumerate(syms, 1):
        d = rd.details(t)
        sic = d.get("sic_code")
        flag = is_commodity(t, sic)
        out[t] = flag
        if flag:
            flagged.append((t, str(sic), (d.get("sic_description") or "")[:34]))
        if i % 200 == 0 or i == len(syms):
            print(f"  {i}/{len(syms)}")
    json.dump(out, open(OUT, "w"))
    print(f"\nWrote {OUT}: {len(out)} tickers, {len(flagged)} flagged commodity "
          f"({len(flagged)/len(out)*100:.0f}%)")
    print("\nEYEBALL these flagged names — veto any that look wrong by editing the rules above:")
    for t, sic, sd in sorted(flagged):
        print(f"  {t:6} SIC {sic:>5}  {sd}")


if __name__ == "__main__":
    main()
