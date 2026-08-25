"""viz_tightflag.py — draw detected HIGH/LOW TIGHT FLAGS so you can eyeball them.

Two bar sources (auto-detected per event, so both piles chart the same way):
  * Polygon 1-min cache -> clock_5min()          (data/events_tightflag.jsonl)
  * IBKR native 5-min   cache/ibkr5/<SYM>/<day>.json  (data/events_tightflag_ibkr.jsonl,
    the SPY/QQQ long history — the only source that reaches back before 2021)


Chart anatomy per detection (5-min clock bars):
  * blue shading  = bar 1 (09:30-09:35) and bar 2 (09:35-09:40), the setup
  * short dashes  = the two bars' highs (the high-cap rule operands)
  * green ▲       = entry (market at 09:40:01, open of the first print >= 09:40)
  * red step line = the ACTIVE stop: bar2 low, then each trailing move
                    (to the low of the previous bar, at a green higher bar's close)
  * purple X      = exit (stop touch; gap-through exits at the open)

Usage:  python viz_tightflag.py <selection.json> <near_misses.json> [out_dir]
        selection.json = {"group": [event, ...]} from data/events_tightflag.jsonl
"""
import json, os, sys
from datetime import date as Date, datetime, timezone
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from research_data import ResearchData
from pattern_detector_tightflag import clock_5min
from data_layer import Bars

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")

rd = ResearchData(os.environ.get("POLYGON_API_KEY", ""))

GREEN, RED = "#26a269", "#c01c28"
IBKR_CACHE = "cache/ibkr5"


def _ibkr_bars(sym: str, day: Date):
    """IBKR native 5-min bars for one day -> (Bars, buckets, cov) like clock_5min.
    cov is reported as 5 (IBKR bars carry no 1-min detail; valid for SPY/QQQ)."""
    p = os.path.join(IBKR_CACHE, sym, day.isoformat() + ".json")
    if not os.path.exists(p):
        return None
    o=[];h=[];l=[];c=[];v=[];ts=[];buckets=[]
    for r in sorted(json.load(open(p)), key=lambda r: r["t"]):
        dt = datetime.fromtimestamp(r["t"]/1000, tz=timezone.utc).astimezone(ET)
        mod = dt.hour*60 + dt.minute
        k = (mod - 570)//5
        if k < 0 or mod >= 960:
            continue
        if buckets and k == buckets[-1]:            # same window -> merge (as clock_5min)
            h[-1] = max(h[-1], r["h"]); l[-1] = min(l[-1], r["l"])
            c[-1] = r["c"]; v[-1] += r["v"]
            continue
        if buckets and k < buckets[-1]:
            continue
        o.append(r["o"]); h.append(r["h"]); l.append(r["l"])
        c.append(r["c"]); v.append(r["v"]); ts.append(dt); buckets.append(k)
    if len(o) < 3:
        return None
    return Bars(sym, day, "5min", ts, o, h, l, c, v, True), buckets, [5]*len(o)


def load_bars(sym: str, day: Date, source: str = "auto"):
    """Load the bars the event was actually COMPUTED from.

    source="ibkr"    -> cache/ibkr5 only (the SPY/QQQ long-history pile)
    source="polygon" -> Polygon 1-min only (the gapper pile)
    source="auto"    -> Polygon if cached, else IBKR

    Getting this wrong draws a chart on a different price series than the one
    tested: the two vendors disagree by 1-3c routinely, and Polygon carries
    occasional phantom prints that stab through the stop line on a trade the
    IBKR record says was never stopped."""
    if source != "ibkr":
        try:
            one = rd.bars(sym, day, "1min")
        except Exception:
            one = None
        if one is not None:
            return clock_5min(one)
        if source == "polygon":
            return None
    return _ibkr_bars(sym, day)


def _candles(ax, five, lo, hi):
    # strictly green (c > o) to match the detector's trail-eligibility color;
    # a doji is drawn red so a chart can't suggest a trail move that didn't happen
    for i in range(lo, hi):
        c = GREEN if five.c[i] > five.o[i] else RED
        ax.plot([i, i], [five.l[i], five.h[i]], color=c, lw=0.9)
        ax.plot([i, i], [five.o[i], five.c[i]], color=c, lw=3.4, solid_capstyle="butt")


def _time_axis(ax, five, lo, hi):
    step = max(1, (hi - lo) // 9)
    ticks = list(range(lo, hi, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([five.ts[i].strftime("%H:%M") for i in ticks], fontsize=8)


def plot_event(e, out, source="auto"):
    day = Date.fromisoformat(e["day"])
    loaded = load_bars(e["symbol"], day, source)
    if loaded is None:
        print("skip (no bars)", e["symbol"], e["day"]); return
    five, buckets, cov = loaded
    side = e.get("side", "long")
    lng = side == "long"
    idx_of = {buckets[i] + 1: i for i in range(len(five))}   # clock bar N -> list idx
    exit_i = idx_of.get(e["exit_bar"], len(five) - 1)
    dayhi_i = idx_of.get(e["day_high_bar"], exit_i)
    lo, hi = 0, min(len(five), max(exit_i + 9, dayhi_i + 3, 14))
    fig, ax = plt.subplots(figsize=(13, 6))
    _candles(ax, five, lo, hi)

    # setup bars + the cap operands (highs for a long, lows for a short)
    ax.axvspan(-0.5, 1.5, alpha=0.10, color="tab:blue")
    ax.annotate("bar1  bar2", (0.5, five.h[0]), textcoords="offset points",
                xytext=(-14, 10), fontsize=9, color="tab:blue", weight="bold")
    lv1, lv2 = (e["b1_h"], e["b2_h"]) if lng else (e["b1_l"], e["b2_l"])
    ax.plot([-0.4, 0.4], [lv1] * 2, "--", color="tab:blue", lw=1.2)
    ax.plot([0.6, 1.4], [lv2] * 2, "--", color="tab:blue", lw=1.2)
    # low-coverage warning: a setup bar built from <5 one-min prints
    for slot, cv in ((0, e["cov1"]), (1, e["cov2"])):
        if cv < 5:
            ax.annotate(f"only {cv}/5 min!", (slot, five.l[slot]),
                        textcoords="offset points", xytext=(-16, -16),
                        fontsize=9, color="darkorange", weight="bold")

    # entry — marker on the bar holding the actual first print (handles delays)
    em = int(e["entry_time"][:2]) * 60 + int(e["entry_time"][3:])
    ent_i = idx_of.get((em - 570) // 5 + 1, min(2, len(five) - 1))
    ax.scatter([ent_i], [e["entry_price"]], marker="^" if lng else "v",
               color=GREEN if lng else "#8b1a86", s=150, zorder=7,
               label=f"entry {side.upper()} {e['entry_price']:.2f} ({e['entry_time']})")

    # active-stop step line: bar2 low from entry, then each trailing move
    steps = [(ent_i, e["stop_price"])]
    for barnum, stop in e.get("trail_path", []):
        i = idx_of.get(barnum)
        if i is not None:
            steps.append((i + 1, stop))          # set at N's close -> active from N+1
    steps.append((exit_i, None))
    for k in range(len(steps) - 1):
        x0, s = steps[k]
        x1, nxt = steps[k + 1]
        # each level ends where the NEXT level becomes active (x1 - 0.4);
        # only the final level extends across the exit bar itself
        x_end = (min(x1, hi - 1) + 0.4) if nxt is None else (min(x1, hi - 1) - 0.4)
        ax.plot([x0 - 0.4, x_end], [s, s], color=RED, lw=1.6,
                ls=":" if k == 0 else "-",
                label=(f"stop (bar2 {'low' if lng else 'high'})") if k == 0 else
                      ("trailing stop" if k == 1 else None))
        if nxt is not None and x1 <= hi - 1:
            ax.plot([x1 - 0.4] * 2, [s, nxt], color=RED, lw=0.9, alpha=0.6)

    # exit + MFE
    ax.scatter([exit_i], [e["exit_price"]], marker="X", color="purple", s=120,
               zorder=8, label=f"exit {e['exit_price']:.2f} ({e['exit_time']})")
    mfe_i = idx_of.get(e["mfe_bar"])
    if mfe_i is not None and mfe_i < hi:
        ax.scatter([mfe_i], [five.h[mfe_i] if lng else five.l[mfe_i]], marker="*",
                   color="goldenrod", s=190,
                   zorder=6, label=f"MFE {e['mfe_R']:+.2f}R")

    g1 = "green" if e["bar1_green"] else "red"
    g2 = "green" if e["bar2_green"] else "red"
    covtxt = f"   cov {e['cov1']}/{e['cov2']} of 5" if (e["cov1"] < 5 or e["cov2"] < 5) else ""
    dlytxt = f"   entry {e['entry_delay_min']}min late" if e["entry_delay_min"] > 0 else ""
    capname = "high_diff" if lng else "low_diff"
    capval = e["high_diff_frac"] if lng else e["low_diff_frac"]
    ax.set_title(
        f"{e['symbol']}  {e['day']}  5min TIGHT FLAG {side.upper()}   |   ratio {e['ratio']:.2f}:1   "
        f"{capname} {capval:+.2f}xR   R=${e['r_unit']:.2f}   "
        f"bar1 {g1} / bar2 {g2}{covtxt}{dlytxt}\n"
        f"{e['exit_reason'].upper()} @ bar {e['exit_bar']}   pnl {e['pnl_R']:+.2f}R   "
        f"MFE {e['mfe_R']:+.2f}R   day-high {e['day_high_R']:+.2f}R   "
        f"{('FLY armed bar ' + str(e['fly_bar'])) if e.get('fly') else 'no fly (stop fixed)'}   "
        f"trail x{e['trail_moves']}   entry-stop dist {e['entry_stop_R']:.2f}R",
        fontsize=10)
    _time_axis(ax, five, lo, hi)
    ax.set_ylabel("price ($, split-adjusted)")
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.15)
    plt.tight_layout(); plt.savefig(out, dpi=95); plt.close()
    print("saved", out)


def plot_near_miss(m, out, source="auto"):
    day = Date.fromisoformat(m["day"])
    loaded = load_bars(m["symbol"], day, source)
    if loaded is None:
        print("skip (no bars)", m["symbol"], m["day"]); return
    five, buckets, cov = loaded
    hi = min(len(five), 14)
    fig, ax = plt.subplots(figsize=(10, 5))
    _candles(ax, five, 0, hi)
    ax.axvspan(-0.5, 1.5, alpha=0.10, color="tab:blue")
    on_low = m["why"] == "low_cap"   # legacy reject reason; the cap rule was deleted 2026-08-06
    ax.plot([-0.4, 0.4], [five.l[0] if on_low else five.h[0]] * 2, "--", color="tab:blue", lw=1.2)
    ax.plot([0.6, 1.4], [five.l[1] if on_low else five.h[1]] * 2, "--", color="tab:blue", lw=1.2)
    why = {
        "ratio": "bar1 only %.2fx bar2 (needs >= 2x)" % m["ratio"],
        "high_cap": "LONG day, bar2 high %+.2fxR above bar1 (cap 0.5xR)" % m.get("high_diff_frac", 0),
        "low_cap": "SHORT day, bar2 low %+.2fxR below bar1 (cap 0.5xR)" % m.get("low_diff_frac", 0),
        "coverage": "setup bars have only %s of 5 minutes traded" % m.get("cov", "?"),
    }[m["why"]]
    ax.set_title(f"{m['symbol']}  {m['day']}  {m.get('side','?').upper()} candidate REJECTED — {why}",
                 fontsize=10, color=RED, weight="bold")
    _time_axis(ax, five, 0, hi)
    ax.set_ylabel("price ($, split-adjusted)")
    ax.grid(alpha=0.15)
    plt.tight_layout(); plt.savefig(out, dpi=95); plt.close()
    print("saved", out)


if __name__ == "__main__":
    sel = json.load(open(sys.argv[1]))
    near = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else []
    out_dir = sys.argv[3] if len(sys.argv) > 3 else "charts/tightflag"
    source = sys.argv[4] if len(sys.argv) > 4 else "auto"   # "ibkr" for the index pile
    os.makedirs(out_dir, exist_ok=True)
    for group, evs in sel.items():
        for e in evs:
            plot_event(e, f"{out_dir}/{group}_{e['symbol']}_{e['day']}.png", source)
    for m in near:
        plot_near_miss(m, f"{out_dir}/nearmiss_{m['why']}_{m['symbol']}_{m['day']}.png", source)
