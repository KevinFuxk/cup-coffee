"""
patch_strict_gap.py — fill the strict pile's missing window
===========================================================
The strict backfill skipped 2023-11-01 .. 2024-06-30 (a connection drop while the screen was
off), leaving events_strict_cup.jsonl with an 8-month hole. This re-runs ONLY that window with
the STRICT (min) rim symmetry — via the rim_symmetry config flag, so the LIVE detector (max/loose)
is untouched — and APPENDS the events to events_strict_cup.jsonl. Resumable (own checkpoint).

After it finishes, re-label the strict backup so the compare panel is fair:
    python rebuild_labels.py    data/events_strict_cup.jsonl data/mined_table_strict_cup.json
    python enrich_real_price.py data/events_strict_cup.jsonl data/realprice_strict_cup.json
"""
from __future__ import annotations
import copy
from datetime import date
from cup_coffee_config_v2 import CONFIG
from run_history_v2 import build_providers
from backfill import run_backfill


def main():
    cfg = copy.deepcopy(CONFIG)
    cfg["pattern"]["rim_symmetry"] = "min"          # STRICT for this patch only; live config stays "max"
    uni, bars = build_providers()
    print("patching STRICT pile: 2023-11-01 -> 2024-06-30  (min rim symmetry, append)")
    totals = run_backfill(cfg, uni, bars, date(2023, 11, 1), date(2024, 6, 30),
                          out_path="data/events_strict_cup.jsonl",
                          checkpoint_path="data/patch_strict.checkpoint", sleep_between=0.0)
    print(f"\nappended {totals['events']} strict events over {totals['days']} days -> events_strict_cup.jsonl")


if __name__ == "__main__":
    main()
