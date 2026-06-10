"""viz_trades.py — draw detected cup-and-handles so you can eyeball them."""
import json, os
from datetime import date as Date
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from research_data import ResearchData

rd = ResearchData(os.environ["POLYGON_API_KEY"])
EVENTS = [json.loads(l) for l in open("data/events.jsonl")]

def find(sym, day, tf):
    for e in EVENTS:
        if e["symbol"].split(":")[-1] == sym and e["day"] == day and e["timeframe"] == tf:
            return e
    return None

def plot(sym, day, tf, out):
    ev = find(sym, day, tf)
    b = rd.bars(ev["symbol"], Date.fromisoformat(day), tf) if ev else None
    if not ev or b is None:
        print("skip", sym, day, tf); return
    cl, cb, cr, bo, ex = ev["cup_left_idx"], ev["cup_bottom_idx"], ev["cup_right_idx"], ev["breakout_idx"], ev["exit_idx"]
    lo_i, hi_i = max(0, cl-5), min(len(b), ex+12)
    fig, ax = plt.subplots(figsize=(13, 6))
    for i in range(lo_i, hi_i):
        c = "#26a269" if b.c[i] >= b.o[i] else "#c01c28"
        ax.plot([i, i], [b.l[i], b.h[i]], color=c, lw=0.8)
        ax.plot([i, i], [b.o[i], b.c[i]], color=c, lw=2.6)
    ax.plot([cl, cb, cr], [b.h[cl], b.l[cb], b.h[cr]], "o", color="black", ms=7, zorder=5)
    ax.annotate("left rim", (cl, b.h[cl]), textcoords="offset points", xytext=(-10, 8), fontsize=9, weight="bold")
    ax.annotate("cup bottom", (cb, b.l[cb]), textcoords="offset points", xytext=(-20, -14), fontsize=9)
    ax.annotate("right rim", (cr, b.h[cr]), textcoords="offset points", xytext=(0, 8), fontsize=9, weight="bold")
    ax.plot([cl, cr], [b.h[cl], b.h[cr]], "k--", alpha=0.45, label="rim line")
    ax.axvspan(cr, bo, alpha=0.12, color="orange"); ax.annotate("handle", ((cr+bo)/2, b.h[cr]), textcoords="offset points", xytext=(-12, 12), fontsize=9, color="darkorange")
    ax.axhline(ev["entry_price"], color="#26a269", ls=":", lw=1.3, label=f"entry {ev['entry_price']:.2f}")
    ax.axhline(ev["stop_price"], color="#c01c28", ls=":", lw=1.3, label=f"stop {ev['stop_price']:.2f}")
    ax.scatter([bo], [ev["entry_price"]], marker="^", color="#26a269", s=160, zorder=6, label="ENTRY")
    out_txt = {1: "WIN", -1: "LOSS", 0: "TIMEOUT"}[ev["outcome"]]
    ax.scatter([ex], [b.c[min(ex, len(b)-1)]], marker="X", color="purple", s=160, zorder=6, label=f"exit ({out_txt})")
    ts0, ts1 = b.ts[bo].strftime("%H:%M"), b.ts[min(ex, len(b)-1)].strftime("%H:%M")
    ax.set_title(f"{sym}  {day}  {tf}   |   {ev.get('reason','')}   |   entry {ts0} -> exit {ts1}   |   {out_txt} {ev['pnl_R']:+.1f}R",
                 fontsize=11)
    ax.set_xlabel("bar number (within the day)"); ax.set_ylabel("price ($)")
    ax.legend(loc="best", fontsize=8); ax.grid(alpha=0.15)
    plt.tight_layout(); plt.savefig(out, dpi=95); plt.close()
    print("saved", out)

if __name__ == "__main__":
    os.makedirs("charts", exist_ok=True)
    for sym, day, tf in [("DAVE","2024-05-07","1min"), ("ARRY","2022-07-28","1min"),
                         ("RBCN","2022-07-05","1min"), ("APRE","2022-07-08","2min")]:
        plot(sym, day, tf, f"charts/{sym}_{day}_{tf}.png")
