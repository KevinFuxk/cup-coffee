"""
study_data_layer.py — SEE what each data-side file produces, for ONE trade (AVXL).
A learning aid: run it, read the labeled output, then open each file knowing what it makes.
    python study_data_layer.py
"""
import os, hashlib
from datetime import date as Date
from cup_coffee_config_v2 import CONFIG          # FILE A: the rules/knobs
from research_data import ResearchData           # FILE B: fetch + cache (calls data_layer)

SYM, DAY = "AVXL", Date(2021, 6, 28)

print("=" * 72)
print("FILE A  cup_coffee_config_v2.py  — the RULES (no action, just settings)")
print("=" * 72)
u = CONFIG["universe"]
print("  always-in whitelist  :", u["whitelist"])
print("  min price / liquidity:", u["scan"]["min_price"], "/", f'{u["scan"]["min_dollar_volume"]:,}')
print("  timeframes detector runs on:", CONFIG["data"]["timeframes"])

print("\n" + "=" * 72)
print("FILE B  research_data.py  — FETCH the bars, and CACHE them to disk")
print("=" * 72)
rd = ResearchData(os.environ["POLYGON_API_KEY"])
one = rd.bars(SYM, DAY, "1min")                  # reads cache/minute/ if present; else downloads ONCE
print(f"  {SYM} {DAY}: {len(one)} one-minute bars  (regular hours only, 9:30-16:00)")
print(f"  first bar {one.ts[0].time()}  O={one.o[0]} H={one.h[0]} L={one.l[0]} C={one.c[0]} V={one.v[0]}")
print(f"  last  bar {one.ts[-1].time()}")
h = hashlib.md5(f"{SYM}:{DAY.isoformat()}".encode()).hexdigest()[:20]
print(f"  now cached at cache/minute/{h}.json  — delete it and the next call re-downloads")

print("\n" + "=" * 72)
print("FILE C  data_layer.py  — RESHAPE: 1-min -> 5-min (one fetch feeds all timeframes)")
print("=" * 72)
five = rd.bars(SYM, DAY, "5min")                 # research_data calls data_layer.downsample() inside
print(f"  {len(one)} one-min bars  ->  {len(five)} five-min bars")
print(f"  first 5-min bar {five.ts[0].time()}  O={five.o[0]} H={five.h[0]} L={five.l[0]} C={five.c[0]}")

print("\nFLOW:  config(who qualifies) -> research_data(fetch+cache) -> data_layer(reshape) -> detector")
print("(FILE D providers_historical.py + FILE E universe.py pick WHICH stocks each day — see notes.)")
