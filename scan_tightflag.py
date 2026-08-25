"""
scan_tightflag.py — run the HIGH/LOW TIGHT FLAG detector over the cached history
================================================================================
Walks a list of (symbol, day) pairs (default: every day in cache/minute — the
same watchlist days the cup-and-handle backfill collected), runs the tight-flag
detector + trailing-stop label on each, and writes the strategy's OWN pile:

    data/events_tightflag.jsonl        (one JSON event per detection)

Reads bars through research_data.ResearchData -> cache/minute, so a fully
cached run needs NO network. Prints the reject funnel and per-year counts.

NOTE: this file belongs to the tight-flag strategy only. It does not touch
events.jsonl or any cup-and-handle file. The frozen-harness evaluation
(real-price screen, costs, verdict table) is a SEPARATE later step — gate (b).

Usage:
    python scan_tightflag.py                       # scan the whole minute cache
    python scan_tightflag.py pairs.json            # scan an explicit pair list
                                                   # (JSON [[sym, "YYYY-MM-DD"], ...])
"""
from __future__ import annotations

import os, sys, json, hashlib
from collections import Counter
from datetime import date as Date, timedelta

from research_data import ResearchData
from pattern_detector_tightflag import scan_day, event_record, CONFIG

OUT = "data/events_tightflag.jsonl"


def cache_pairs() -> list[tuple[str, str]]:
    """Recover (symbol, day) for every file in cache/minute by reversing the
    md5(key)[:20] filename against candidate symbols (union of all events
    piles) x weekdays. Coverage is ~98%; the remainder are symbols that never
    produced an event in any pile (unrecoverable from the hash alone).
    CAVEAT: that residual ~2% exclusion is conditioned on the symbol producing
    a cup event SOMEWHERE in 2021-2026 (mild future-information filter on
    which watchlist days get scanned). Fix = scan from an explicit pair list
    (the manual universe) instead of hash recovery."""
    syms = set()
    for fn in os.listdir("data"):
        if fn.startswith("events") and fn.endswith(".jsonl"):
            with open(os.path.join("data", fn)) as f:
                for line in f:
                    try:
                        s = json.loads(line).get("symbol", "")
                    except Exception:
                        continue
                    if s:
                        syms.add(s.split(":")[-1])
    files = {fn[:-5] for fn in os.listdir("cache/minute") if fn.endswith(".json")}
    d, end = Date(2021, 1, 1), Date.today()
    days = []
    while d <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    pairs = []
    for s in syms:
        for dy in days:
            if hashlib.md5(f"{s}:{dy}".encode()).hexdigest()[:20] in files:
                pairs.append((s, dy))
    pairs.sort(key=lambda p: (p[1], p[0]))
    print(f"cache pairs: {len(pairs)} recovered from {len(files)} files "
          f"({len(pairs)/len(files)*100:.1f}%)")
    return pairs


def main():
    if len(sys.argv) > 1:
        pairs = [tuple(p) for p in json.load(open(sys.argv[1]))]
        print(f"pairs from {sys.argv[1]}: {len(pairs)}")
    else:
        pairs = cache_pairs()

    rd = ResearchData(os.environ.get("POLYGON_API_KEY", ""))
    funnel = Counter()
    per_year = Counter()
    n_events = 0
    with open(OUT, "w") as fout:
        for i, (sym, dy) in enumerate(pairs, 1):
            day = Date.fromisoformat(dy)
            one = rd.bars(sym, day, "1min")
            if one is None:
                funnel["no_bars"] += 1
                continue
            ev, why = scan_day(one, sym, day)
            if ev is None:
                funnel[why] += 1
                continue
            funnel["DETECTED"] += 1
            per_year[dy[:4]] += 1
            fout.write(json.dumps(event_record(ev)) + "\n")
            n_events += 1
            if i % 1000 == 0:
                print(f"  {i}/{len(pairs)}  events so far: {n_events}")

    print(f"\nwrote {OUT}: {n_events} events from {len(pairs)} (sym, day) pairs")
    print("\nfunnel (why days were rejected):")
    for k, v in funnel.most_common():
        print(f"  {k:<14} {v}")
    print("\ndetections per year:")
    for y in sorted(per_year):
        print(f"  {y}  {per_year[y]}")
    print(f"\nconfig: ratio_min={CONFIG['ratio_min']}  "
          f"min_coverage={CONFIG['min_coverage']}  (overshoot cap removed 2026-08-06)")


if __name__ == "__main__":
    main()
