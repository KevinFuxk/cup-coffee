"""build_nb.py — generate data_cleaning_walkthrough.ipynb (a teaching notebook)."""
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)
import nbformat as nbf

nb = nbf.v4.new_notebook()
cells = []
def md(s):   cells.append(nbf.v4.new_markdown_cell(s))
def code(s): cells.append(nbf.v4.new_code_cell(s))

md("""# Cup Coffee — Data Cleaning, step by step

Run each cell top-to-bottom and **watch the data change**. The goal is to *see* exactly what
"cleaning" does to the raw market data before it ever reaches the strategy.

Pipeline we'll walk:
`raw Polygon bars → keep regular hours → downsample → the trade table → drop tiny-stop noise →
fix reverse-split ghost prices → flag commodity names → charge real costs → the clean set.`

> Run this from the project root so `import research_data` works. The bar cells read the
> on-disk cache (need `POLYGON_API_KEY` in your env; cached, so no real download).""")

code("""import os, json
import pandas as pd
from datetime import date as Date, datetime, timezone
pd.set_option("display.max_columns", 40)
pd.set_option("display.width", 160)

DATA = "data"
def load_jsonl(p): return [json.loads(l) for l in open(p) if l.strip()]
def key(e):       # the join key used across every data file
    return f'{e["symbol"]}|{e["day"]}|{e["timeframe"]}|{e.get("breakout_idx")}|{e.get("handle_num")}'
print("setup ready")""")

md("""## 1. Raw Polygon bars → clean candles  (`data_layer.py` + `research_data.py`)

Polygon hands us **every** minute of the session — including pre-market and after-hours. Step 1
of cleaning is to keep only **regular trading hours (09:30–16:00 ET)**.""")

code("""from research_data import ResearchData, ET
rd = ResearchData(os.environ["POLYGON_API_KEY"])
SYM, DAY = "AVXL", Date(2021, 6, 28)              # our running example trade

raw = rd._minute_raw(SYM, DAY)                    # RAW cached Polygon rows (all sessions)
rawdf = pd.DataFrame(raw)
rawdf["time_ET"] = [datetime.fromtimestamp(t/1000, tz=timezone.utc).astimezone(ET).strftime("%H:%M")
                    for t in rawdf["t"]]
print(f"RAW rows from Polygon: {len(rawdf)}   (first {rawdf['time_ET'].iloc[0]}  last {rawdf['time_ET'].iloc[-1]})")
rawdf[["time_ET","o","h","l","c","v"]].head()""")

code("""# After the regular-hours filter (this is what research_data.bars() returns):
one = rd.bars(SYM, DAY, "1min")
print(f"RAW {len(rawdf)} rows  ->  REGULAR-HOURS {len(one)} one-minute bars")
print(f"   (dropped {len(rawdf)-len(one)} pre/after-hours bars; kept {one.ts[0].time()}–{one.ts[-1].time()})")""")

md("""### Downsampling: 1-min → 5-min
Each 5-minute candle = **open** of the first minute, **max high**, **min low**, **close** of the
last minute, **summed volume**. Watch five 1-min bars collapse into one.""")

code("""five = rd.bars(SYM, DAY, "5min")
print(f"{len(one)} one-min bars  ->  {len(five)} five-min bars\\n")
first5 = pd.DataFrame({"time":[t.strftime('%H:%M') for t in one.ts[:5]],
                       "o":one.o[:5], "h":one.h[:5], "l":one.l[:5], "c":one.c[:5], "v":one.v[:5]})
print("the first FIVE 1-min bars:"); display(first5)
collapsed = pd.DataFrame({"time":[five.ts[0].strftime('%H:%M')], "o":[five.o[0]], "h":[five.h[0]],
                          "l":[five.l[0]], "c":[five.c[0]], "v":[five.v[0]]})
print("collapse to ONE 5-min bar  (open=first, high=max, low=min, close=last, vol=sum):")
display(collapsed)""")

md("""## 2. The trade pile as a table
`events.jsonl` is one row per detected trade. Load it into a DataFrame — this is the raw,
labeled dataset everything else operates on.""")

code("""events = load_jsonl(f"{DATA}/events.jsonl")
ev = pd.DataFrame(events)
print("trades in the pile:", len(ev))
ev[["symbol","day","timeframe","reason","size_tier","entry_price","stop_price","risk_R","outcome","pnl_R"]].head(8)""")

md("""## 3. Cleaning #1 — drop the tiny-stop noise
`R = entry − stop`. As a % of price, a *tiny* stop (handle ≈ 0) makes nonsense R-multiples and
gets crushed by fixed costs. Look at the distribution, then the offenders.""")

code("""ev["stop_pct"] = ev["risk_R"] / ev["entry_price"] * 100
print("stop, as % of price — distribution:")
print(ev["stop_pct"].describe()[["min","25%","50%","75%","max"]])
print("\\nthe tiniest stops (sub-noise — mostly index trades):")
ev.sort_values("stop_pct")[["symbol","day","entry_price","risk_R","stop_pct"]].head(6)""")

code("""MIN_STOP = 0.25                                   # %
keep = ev["stop_pct"] >= MIN_STOP
print(f"min-stop {MIN_STOP}% filter:  {len(ev)}  ->  {keep.sum()} trades   (dropped {len(ev)-keep.sum()})")""")

md("""## 4. Cleaning #2 — fix the reverse-split *ghost* prices  (the big one)
Polygon prices are split-**adjusted**, so a stock that later reverse-split shows a wildly inflated
history. `realprice.json` (from `enrich_real_price.py`) recovers the **real** price. Watch the
before/after.""")

code("""RP = json.load(open(f"{DATA}/realprice.json"))
ev["real_price"] = ev.apply(lambda r: RP.get(key(r), {}).get("real_price", r["entry_price"]), axis=1)
ev["real_risk"]  = ev.apply(lambda r: RP.get(key(r), {}).get("real_risk",  r["risk_R"]),       axis=1)
ghosts = ev[ev["entry_price"] > 2000].sort_values("entry_price", ascending=False)
print(f"{len(ghosts)} trades carry split-inflated prices. The worst offenders (adjusted vs REAL):")
ghosts[["symbol","day","entry_price","real_price","risk_R","real_risk"]].head(8)""")

code("""# Why it matters: a fixed fee divided by the FAKE dollar-risk looks ~free; on the REAL risk it's brutal.
FEE = (2.0 + 2*0.3) / 100.0                       # $0.026: 2c slippage + 0.6c round-trip commission
ev["cost_adj_R"]  = FEE / ev["risk_R"]            # WRONG — cost on the inflated price
ev["cost_real_R"] = FEE / ev["real_risk"]         # RIGHT — cost on the real price
print("cost in R: on the fake price (≈0) vs the real price (huge):")
ghosts2 = ev[ev["entry_price"] > 2000]
ghosts2[["symbol","entry_price","real_price","cost_adj_R","cost_real_R"]].head(6)""")

code("""MIN_PRICE = 15                                    # $, on the REAL price
keep_price = ev["real_price"] >= MIN_PRICE
print(f"min REAL price ${MIN_PRICE} filter:  {len(ev)}  ->  {keep_price.sum()}   (dropped {len(ev)-keep_price.sum()} penny names)")""")

md("""## 5. Cleaning #3 — flag commodity-sector names  (`enrich_commodity.py`, SIC-based)""")

code("""COMM = json.load(open(f"{DATA}/commodity.json"))
ev["commodity"] = ev["symbol"].str.split(":").str[-1].map(lambda s: COMM.get(s, False))
print("commodity-sector trades flagged:", int(ev["commodity"].sum()), "of", len(ev))
ev[ev["commodity"]][["symbol","day","reason"]].drop_duplicates("symbol").head(10)""")

md("""## 6. Charge real costs, then assemble the CLEAN set
Apply every screen (min-stop + min-real-price), then compute **net** R at a take-profit using the
full-path labels (`mined_table.json`) minus the **real** cost.""")

code("""TAB = {r["key"]: r for r in json.load(open(f"{DATA}/mined_table.json")) if "key" in r}
clean = ev[(ev["stop_pct"] >= MIN_STOP) & (ev["real_price"] >= MIN_PRICE)].copy()
def net_at(r, k):
    m = TAB.get(key(r))
    return None if not m else m["realized_R"][str(k)] - FEE / r["real_risk"]
clean["net_8R"] = clean.apply(lambda r: net_at(r, 8), axis=1)

print(f"RAW pile        : {len(ev):>6} trades")
print(f"CLEAN pile      : {len(clean):>6} trades  (after min-stop {MIN_STOP}% + min-real-price ${MIN_PRICE})")
print(f"\\nCLEAN @ 8R, net of REAL costs: total {clean['net_8R'].sum():+.0f}R | "
      f"avg {clean['net_8R'].mean():+.3f}R/trade | win {100*(clean['net_8R']>0).mean():.0f}%")""")

md("""## Recap — the cleaning pipeline
1. **Raw bars → regular hours** (drop pre/after-market)
2. **Downsample** 1→5-min (open/maxH/minL/close/sumV)
3. **Trade table** (one row per detected trade)
4. **Tiny-stop screen** — remove sub-noise R-multiples
5. **Real-price fix** — undo split inflation; recover true price & dollar-risk
6. **Commodity flag** — SIC-based sector tag
7. **Real-cost net R** — the honest, analyzable dataset

Every later step (factor mining, the dashboard, ML) runs on the **clean** set, not the raw pile.
Change `MIN_STOP`, `MIN_PRICE`, or `FEE` above and re-run to watch the clean set move.""")

nb["cells"] = cells
nb["metadata"] = {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
                  "language_info": {"name": "python"}}
nbf.write(nb, "tools/data_cleaning_walkthrough.ipynb")
print(f"wrote data_cleaning_walkthrough.ipynb  ({len(cells)} cells)")
