"""
rebuild_labels.py — fast re-label of the current pile for the DASHBOARD
=======================================================================
Recomputes mined_table.json directly from events.jsonl + cached bars: the realized R at
every take-profit 1R..20R, MFE, win flags, and entry_min (for the Session filter). This is
the dashboard-complete table — it does NOT compute the 28 mining factors. If you want factor
SCORING, run `python mine_factors.py --rebuild` separately (slower; recomputes factors too).

Reads cached bars -> fast, no re-download. Run after a backfill changes events.jsonl.
    python rebuild_labels.py
"""
from __future__ import annotations
import os, json
from datetime import date as Date
from research_data import ResearchData, label_full_path

EVENTS = "data/events.jsonl"
OUT = "data/mined_table.json"
TPS = tuple(range(1, 21))


def key(e):
    return f'{e["symbol"]}|{e["day"]}|{e["timeframe"]}|{e.get("breakout_idx")}|{e.get("handle_num")}'


def main():
    rd = ResearchData(os.environ["POLYGON_API_KEY"])
    events = [json.loads(l) for l in open(EVENTS) if l.strip()]
    rows, skip = [], 0
    for i, e in enumerate(events, 1):
        b = rd.bars(e["symbol"], Date.fromisoformat(e["day"]), e["timeframe"])
        if b is None:
            skip += 1
            continue
        bi = min(e["breakout_idx"], len(b) - 1)
        lab = label_full_path(b, bi, e["entry_price"], e["stop_price"], e["risk_R"], take_profits=TPS)
        t = b.ts[bi].time()
        rows.append({"key": key(e), "symbol": e["symbol"], "day": e["day"], "tier": e.get("size_tier"),
                     "realized_R": lab["realized_R"], "win": lab["win"], "full_mfe_R": lab["full_mfe_R"],
                     "entry_min": t.hour * 60 + t.minute})
        if i % 2000 == 0:
            print(f"  {i}/{len(events)}")
    json.dump(rows, open(OUT, "w"))
    print(f"\nwrote {OUT}: {len(rows)} rows (skipped {skip})")


if __name__ == "__main__":
    main()
