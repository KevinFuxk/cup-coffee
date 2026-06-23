"""
enrich_entry_type.py — tag each trade: momentum ("flag") vs consolidation (true cup-and-handle)
===============================================================================================
The detector already makes this split inside _find_handle: after the cup's right rim
(cup_right_idx = ri), if the NEXT bar already makes a new high (b.h[ri+1] >= b.h[ri]) the
entry is MOMENTUM — price re-broke immediately with no handle to wait for (your "high tight
flag" / incomplete-handle case). Otherwise price pulled back into a real handle = CONSOLIDATION
(the classic cup-and-handle).

That flag was never stored in events.jsonl. This re-derives it from the CACHED bars (no
re-backfill) and writes data/entry_type.json = { event_key: "momentum" | "consolidation" } so
the dashboard can compare the two and drop the momentum path on demand.

Run in your terminal (POLYGON_API_KEY from ~/.zshrc). Bars are cached -> fast.
    python enrich_entry_type.py
"""
from __future__ import annotations
import os, json
from datetime import date as Date
from research_data import ResearchData

OUT = "data/entry_type.json"
EVENTS = "data/events.jsonl"


def key(e):
    return f'{e["symbol"]}|{e["day"]}|{e["timeframe"]}|{e.get("breakout_idx")}|{e.get("handle_num")}'


def main():
    rd = ResearchData(os.environ["POLYGON_API_KEY"])
    events = [json.loads(l) for l in open(EVENTS) if l.strip()]
    out = {}
    mom = con = skip = 0
    for i, e in enumerate(events, 1):
        ri = e.get("cup_right_idx")
        b = rd.bars(e["symbol"], Date.fromisoformat(e["day"]), e["timeframe"])
        if b is None or ri is None or ri + 1 >= len(b):
            out[key(e)] = None
            skip += 1
            continue
        momentum = b.h[ri + 1] >= b.h[ri] - 1e-9      # SAME test the detector uses
        out[key(e)] = "momentum" if momentum else "consolidation"
        mom += momentum
        con += (not momentum)
        if i % 2000 == 0:
            print(f"  {i}/{len(events)}")
    json.dump(out, open(OUT, "w"))
    tot = mom + con
    print(f"\nWrote {OUT}: {len(out)} trades")
    print(f"  momentum (flag / no handle)      : {mom}  ({mom/tot*100:.0f}%)")
    print(f"  consolidation (true cup+handle)  : {con}  ({con/tot*100:.0f}%)")
    print(f"  skipped (no bars / no rim idx)   : {skip}")


if __name__ == "__main__":
    main()
