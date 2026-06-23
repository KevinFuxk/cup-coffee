"""
enrich_cup_strictness.py — tag each trade STRICT vs LOOSE cup (rim symmetry)
===========================================================================
The detector's rim-symmetry guard (pattern_detector._find_handle) keeps a cup only if
    abs(rim - left_lip) < rim_recov * MAX(left_lip - cup_low, rim - cup_low)     # LOOSE (current)
A STRICTER cup swaps MAX for MIN — the two rim-to-bottom depths must be closer, so the cup
is more symmetric (fewer, cleaner cups pass):
    abs(rim - left_lip) < rim_recov * MIN(left_lip - cup_low, rim - cup_low)     # STRICT

Every current event already passed the LOOSE check. This re-derives, from the CACHED bars
(no re-backfill), which ones ALSO pass the STRICT check, and writes
    data/cup_strict.json = { event_key: true/false }

CAVEAT: this is a SUBSET view (strict ⊆ loose) — "of the trades we take now, which survive a
tighter rim-symmetry." A true strict re-run could find a few *different* cups (rejecting one
cup can surface another nearby), so to ADOPT strict as the live rule you'd flip max->min in
pattern_detector.py and re-run the backfill. For comparing the two, this subset is the honest read.

Run in your terminal (POLYGON_API_KEY from ~/.zshrc). Bars are cached -> fast.
    python enrich_cup_strictness.py
"""
from __future__ import annotations
import os, json
from datetime import date as Date
from research_data import ResearchData
from cup_coffee_config_v2 import CONFIG

OUT = "data/cup_strict.json"
EVENTS = "data/events.jsonl"
RIM_RECOV = CONFIG["pattern"]["right_rim_recovery_frac"]   # 0.25


def key(e):
    return f'{e["symbol"]}|{e["day"]}|{e["timeframe"]}|{e.get("breakout_idx")}|{e.get("handle_num")}'


def main():
    rd = ResearchData(os.environ["POLYGON_API_KEY"])
    events = [json.loads(l) for l in open(EVENTS) if l.strip()]
    out = {}
    strict = loose = skip = 0
    for i, e in enumerate(events, 1):
        li, ri, bi = e.get("cup_left_idx"), e.get("cup_right_idx"), e.get("cup_bottom_idx")
        b = rd.bars(e["symbol"], Date.fromisoformat(e["day"]), e["timeframe"])
        if b is None or None in (li, ri, bi) or max(li, ri, bi) >= len(b):
            out[key(e)] = None; skip += 1; continue
        left_lip, rim, cup_low = b.h[li], b.h[ri], b.l[bi]
        is_strict = abs(rim - left_lip) < RIM_RECOV * min(left_lip - cup_low, rim - cup_low)
        out[key(e)] = bool(is_strict)
        strict += is_strict; loose += (not is_strict)
        if i % 2000 == 0:
            print(f"  {i}/{len(events)}")
    json.dump(out, open(OUT, "w"))
    tot = strict + loose
    print(f"\nWrote {OUT}: {len(out)} trades  (rim_recov={RIM_RECOV})")
    print(f"  STRICT cup (passes min-symmetry): {strict}  ({strict/tot*100:.0f}%)")
    print(f"  LOOSE only (max but not min)    : {loose}  ({loose/tot*100:.0f}%)")
    print(f"  skipped                          : {skip}")


if __name__ == "__main__":
    main()
