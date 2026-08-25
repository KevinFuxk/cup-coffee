"""viz_batch.py — generate a diverse batch of detected cup-and-handle charts to eyeball."""
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)
import os
from viz_trades import plot, EVENTS

os.makedirs("charts", exist_ok=True)
# real-priced only (drop split-adjusted absurd values so axes look right)
real = [e for e in EVENTS if 1 <= e["entry_price"] <= 1500]
wins   = sorted([e for e in real if e["outcome"] == 1],  key=lambda e: -e["pnl_R"])[:7]
times  = sorted([e for e in real if e["outcome"] == 0],  key=lambda e: -e["pnl_R"])[:4]
losses = [e for e in real if e["outcome"] == -1][:5]
earn   = [e for e in real if e.get("reason") == "earnings_gap"][:6]

sel = {}
for e in wins + times + losses + earn:
    sel[(e["symbol"], e["day"], e["timeframe"])] = e   # dedup by trade
for (sym, day, tf) in sel:
    plot(sym.split(":")[-1], day, tf, f"charts/{sym.split(':')[-1]}_{day}_{tf}.png")
print(f"\ngenerated {len(sel)} charts in charts/")
