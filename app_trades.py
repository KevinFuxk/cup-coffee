"""
app_trades.py — focused trade viewer: costs · MFE · cumulative · trade pictures
===============================================================================
A deliberately small companion to app.py (which stays untouched). Four things only:

  1. cost sliders        — commission ¢/share each-way + entry slippage ¢
  2. MFE                 — peak favourable excursion, per trade and as a distribution
  3. cumulative graph    — equity in R over the period, recomputed live from the sliders
  4. trade pictures      — candlestick charts of the actual detections, cup markers drawn

    streamlit run app_trades.py                       # default pile
    PILE=data/events_fixedrim.jsonl streamlit run app_trades.py

Charts render from the local bar cache, so no API calls and no rate limits.
"""
from __future__ import annotations

import json
import os
from datetime import date as Date

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import streamlit as st

from research_data import ResearchData

PILE = os.environ.get("PILE", "data/events_fixedrim.jsonl")
TABLE = os.environ.get("TABLE", "data/mined_table_fixedrim.json")
RP = os.environ.get("RP", "data/realprice_fixedrim.json")

st.set_page_config(page_title="Cup & Handle — trades", layout="wide")
st.title("🫖 Cup & Handle — trade viewer")


def key(e):
    return f'{e["symbol"]}|{e["day"]}|{e["timeframe"]}|{e.get("breakout_idx")}|{e.get("handle_num")}'


@st.cache_data
def load(pile, table, rp):
    events = [json.loads(l) for l in open(pile) if l.strip()]
    tab = {r["key"]: r for r in json.load(open(table)) if "key" in r} if os.path.exists(table) else {}
    prices = json.load(open(rp)) if os.path.exists(rp) else {}
    rows = []
    for e in events:
        m = tab.get(key(e))
        if not m:
            continue
        p = prices.get(key(e), {})
        rows.append({**e,
                     "realized_R": m["realized_R"], "full_mfe_R": m.get("full_mfe_R", 0.0),
                     "real_price": p.get("real_price", e.get("entry_price") or 0),
                     "real_risk": p.get("real_risk") or e["risk_R"]})
    return rows


rows = load(PILE, TABLE, RP)
if not rows:
    st.error(f"no labelled trades found in {PILE}")
    st.stop()

syms = sorted({r["symbol"].split(":")[-1] for r in rows})
idx_share = sum(1 for r in rows if r["symbol"].split(":")[-1] in ("SPY", "QQQ")) / len(rows)
st.caption(f"pile **{PILE}** · {len(rows):,} labelled trades · "
           f"{min(r['day'] for r in rows)} → {max(r['day'] for r in rows)} · "
           f"{len(syms)} symbols · index share {idx_share*100:.0f}%")
if idx_share > 0.9:
    st.warning("⚠️ This pile is almost entirely SPY/QQQ — the universe screen failed when it was built. "
               "Index-only results are known-negative and not representative of the gapper strategy.")

# ---------------- 1. cost sliders ----------------
st.sidebar.header("💵 Costs")
comm = st.sidebar.slider("Commission ¢/share (each way)", 0.0, 2.0, 0.3, 0.1)
slip = st.sidebar.slider("Entry slippage ¢", 0.0, 10.0, 2.0, 0.5)
tp = st.sidebar.select_slider("Take-profit (R)", options=list(range(1, 21)), value=6)
st.sidebar.header("🔎 Filters")
minprice = st.sidebar.slider("Min real price $", 0.0, 50.0, 15.0, 1.0)
minstop = st.sidebar.slider("Min stop (% of price)", 0.0, 2.0, 0.25, 0.05)
drop_index = st.sidebar.checkbox("Exclude SPY/QQQ", value=False)

FEE = (slip + 2 * comm) / 100.0
stop_pct = lambda r: (r["risk_R"] / r["entry_price"] * 100) if r.get("entry_price") else 0

sel = [r for r in rows
       if r["real_price"] >= minprice and stop_pct(r) >= minstop and r["real_risk"] > 0
       and not (drop_index and r["symbol"].split(":")[-1] in ("SPY", "QQQ"))]
for r in sel:
    r["net"] = r["realized_R"][str(tp)] - FEE / r["real_risk"]

if not sel:
    st.error("no trades pass the current filters")
    st.stop()

total = sum(r["net"] for r in sel)
wins = sum(1 for r in sel if r["net"] > 0)
mfes = sorted(r["full_mfe_R"] for r in sel)
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Trades", f"{len(sel):,}")
c2.metric("Total R", f"{total:+.1f}")
c3.metric("R / trade", f"{total/len(sel):+.3f}")
c4.metric("Win rate", f"{wins/len(sel)*100:.0f}%")
c5.metric("Median MFE", f"{mfes[len(mfes)//2]:.2f}R")

# ---------------- 3. cumulative graph ----------------
st.subheader("Cumulative R — recomputed from the cost sliders")
sel.sort(key=lambda r: (r["day"], r.get("entry_min") or 0))
cum, running = [], 0.0
for r in sel:
    running += r["net"]
    cum.append(running)
fig, ax = plt.subplots(figsize=(12, 3.6))
ax.plot(range(len(cum)), cum, lw=1.6, color="#c01c28" if running < 0 else "#26a269")
ax.axhline(0, color="#888", lw=1)
ax.set_xlabel("trade #"); ax.set_ylabel("cumulative R")
ax.grid(alpha=.25)
step = max(1, len(sel) // 8)
ax.set_xticks(range(0, len(sel), step))
ax.set_xticklabels([sel[i]["day"][:7] for i in range(0, len(sel), step)], fontsize=8)
st.pyplot(fig, clear_figure=True)

# ---------------- 2. MFE distribution ----------------
st.subheader("MFE — how far trades ran in your favour at best")
f2, a2 = plt.subplots(figsize=(12, 2.8))
a2.hist([min(x, 15) for x in mfes], bins=60, color="#3584e4")
a2.axvline(tp, color="#c01c28", ls="--", lw=1.5, label=f"take-profit {tp}R")
a2.set_xlabel("peak favourable excursion (R, capped at 15)"); a2.set_ylabel("trades")
a2.legend(); a2.grid(alpha=.25)
st.pyplot(f2, clear_figure=True)
never1 = sum(1 for x in mfes if x < 1) / len(mfes) * 100
reached = sum(1 for x in mfes if x >= tp) / len(mfes) * 100
st.caption(f"{never1:.0f}% of trades never reach +1R · {reached:.0f}% reach the {tp}R target")

# ---------------- 4. trade pictures ----------------
st.subheader("Trade pictures")
colf1, colf2, colf3 = st.columns(3)
sym_pick = colf1.selectbox("Symbol", ["(all)"] + sorted({r["symbol"].split(":")[-1] for r in sel}))
sort_by = colf2.selectbox("Show", ["biggest winners", "biggest losers", "most recent", "highest MFE"])
n_show = colf3.slider("How many", 3, 24, 6, 3)

view = [r for r in sel if sym_pick == "(all)" or r["symbol"].split(":")[-1] == sym_pick]
view.sort(key={"biggest winners": lambda r: -r["net"],
               "biggest losers": lambda r: r["net"],
               "most recent": lambda r: (r["day"], r.get("entry_min") or 0),
               "highest MFE": lambda r: -r["full_mfe_R"]}[sort_by],
          reverse=(sort_by == "most recent"))
view = view[:n_show]


@st.cache_resource
def _rd():
    return ResearchData(os.environ["POLYGON_API_KEY"])


def draw(e):
    """Candlestick of the detection: cup markers, trigger and stop lines. Cached bars only."""
    b = _rd().bars(e["symbol"], Date.fromisoformat(e["day"]), e["timeframe"])
    if b is None:
        return None
    cl, cb, cr = e["cup_left_idx"], e["cup_bottom_idx"], e["cup_right_idx"]
    bo, ex = e["breakout_idx"], min(e.get("exit_idx") or e["breakout_idx"], len(b) - 1)
    lo, hi = max(0, cl - 5), min(len(b), ex + 10)
    fig, ax = plt.subplots(figsize=(7, 3.4))
    for i in range(lo, hi):
        col = "#26a269" if b.c[i] >= b.o[i] else "#c01c28"
        ax.plot([i, i], [b.l[i], b.h[i]], color=col, lw=0.8)
        ax.plot([i, i], [b.o[i], b.c[i]], color=col, lw=2.4)
    ax.plot([cl, cb, cr], [b.h[cl], b.l[cb], b.h[cr]], "o", color="black", ms=6, zorder=5)
    ax.plot([cl, cr], [b.h[cl], b.h[cr]], "--", color="#666", lw=1)
    ax.axhline(e["entry_price"], color="#1a7f37", lw=1.2, ls="--")
    ax.axhline(e["stop_price"], color="#c01c28", lw=1.2, ls="--")
    ax.axvline(bo, color="#1a7f37", lw=1, alpha=.5)
    ax.set_xticks([]); ax.grid(alpha=.2)
    ax.set_title(f"{e['symbol'].split(':')[-1]} {e['day']} {e['timeframe']} — "
                 f"{e['net']:+.2f}R (MFE {e['full_mfe_R']:.1f}R)", fontsize=10)
    return fig


cols = st.columns(3)
for i, e in enumerate(view):
    with cols[i % 3]:
        fig = draw(e)
        if fig is None:
            st.caption(f"{e['symbol']} {e['day']} — bars not in cache")
        else:
            st.pyplot(fig, clear_figure=True)
            st.caption(f"entry ${e['entry_price']:.2f} · stop ${e['stop_price']:.2f} · "
                       f"risk {stop_pct(e):.2f}% · net {e['net']:+.2f}R")
