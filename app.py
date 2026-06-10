"""
app.py — Cup & Handle Trade Explorer (interactive dashboard)
Run:  streamlit run app.py
Filter all detected trades and chart any one with its cup/handle/entry/stop marked.
"""
import json, os
from datetime import date as Date
import streamlit as st
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from research_data import ResearchData

st.set_page_config(layout="wide", page_title="Cup & Handle Trade Explorer")
V = {1: "WIN", -1: "LOSS", 0: "TIME"}


@st.cache_resource
def get_rd():
    return ResearchData(os.environ["POLYGON_API_KEY"])

@st.cache_data
def load_events():
    return [json.loads(l) for l in open("data/events.jsonl")]

@st.cache_data
def load_table():
    return {r["key"]: r for r in json.load(open("data/mined_table.json")) if "key" in r}

rd = get_rd()
events = load_events()
TP = load_table()

def tp_of(e):
    k = f"{e['symbol']}|{e['day']}|{e['timeframe']}|{e.get('breakout_idx')}|{e.get('handle_num')}"
    return TP.get(k)


def make_fig(e):
    b = rd.bars(e["symbol"], Date.fromisoformat(e["day"]), e["timeframe"])
    if b is None:
        return None
    cl, cb, cr, bo, ex = e["cup_left_idx"], e["cup_bottom_idx"], e["cup_right_idx"], e["breakout_idx"], e["exit_idx"]
    lo_i, hi_i = max(0, cl - 5), min(len(b), ex + 12)
    fig, ax = plt.subplots(figsize=(13, 6))
    for i in range(lo_i, hi_i):
        c = "#26a269" if b.c[i] >= b.o[i] else "#c01c28"
        ax.plot([i, i], [b.l[i], b.h[i]], color=c, lw=0.8)
        ax.plot([i, i], [b.o[i], b.c[i]], color=c, lw=2.6)
    ax.plot([cl, cb, cr], [b.h[cl], b.l[cb], b.h[cr]], "o", color="black", ms=7, zorder=5)
    ax.annotate("left rim", (cl, b.h[cl]), textcoords="offset points", xytext=(-10, 8), fontsize=9, weight="bold")
    ax.annotate("cup bottom", (cb, b.l[cb]), textcoords="offset points", xytext=(-20, -14), fontsize=9)
    ax.annotate("right rim", (cr, b.h[cr]), textcoords="offset points", xytext=(0, 8), fontsize=9, weight="bold")
    ax.plot([cl, cr], [b.h[cl], b.h[cr]], "k--", alpha=0.45)
    ax.axvspan(cr, bo, alpha=0.12, color="orange")
    ax.axhline(e["entry_price"], color="#26a269", ls=":", lw=1.3, label=f"entry {e['entry_price']:.2f}")
    ax.axhline(e["stop_price"], color="#c01c28", ls=":", lw=1.3, label=f"stop {e['stop_price']:.2f}")
    ax.scatter([bo], [e["entry_price"]], marker="^", color="#26a269", s=160, zorder=6, label="ENTRY")
    m = tp_of(e)
    ax.scatter([ex], [b.c[min(ex, len(b)-1)]], marker="X", color="purple", s=160, zorder=6, label="end of hold")
    mfe_txt = f"ran to {m['full_mfe_R']:.1f}R (MFE)" if m else ""
    ax.set_title(f"{e['symbol'].split(':')[-1]}  {e['day']}  {e['timeframe']}   |   {e.get('reason','')}   |   {mfe_txt}")
    ax.legend(loc="best", fontsize=8); ax.grid(alpha=0.15)
    ax.set_xlabel("bar # (within the day)"); ax.set_ylabel("price ($)")
    plt.tight_layout()
    return fig


st.title("🫖 Cup & Handle — Trade Explorer")
st.caption(f"{len(events)} detected trades across the 4-year backtest")

sb = st.sidebar
sb.header("Filters")
outc = sb.multiselect("Outcome", ["WIN", "LOSS", "TIME"], ["WIN", "LOSS", "TIME"])
reasons = sorted({e.get("reason", "") for e in events})
rsel = sb.multiselect("Catalyst", reasons, reasons)
tfs = sb.multiselect("Timeframe", ["1min", "2min", "5min"], ["1min", "2min", "5min"])
tiers = sorted({e["size_tier"] for e in events})
tsel = sb.multiselect("Size tier", tiers, tiers)
symq = sb.text_input("Symbol contains").upper()
rrng = sb.slider("P/L (R)", -2.0, 20.0, (-2.0, 20.0))
minstop = sb.slider("Min stop (% of price)", 0.0, 3.0, 0.0, 0.05,
                    help="0 = ALL trades (raw pile, incl. broken R≈0 rows). Raise it to drop sub-noise tiny-stop patterns.")


def keep(e):
    sp = (e["risk_R"] / e["entry_price"] * 100) if e.get("entry_price") else 0.0
    return (V[e["outcome"]] in outc and e.get("reason", "") in rsel and e["timeframe"] in tfs
            and e["size_tier"] in tsel and symq in e["symbol"].upper()
            and rrng[0] <= e["pnl_R"] <= rrng[1] and sp >= minstop)

filt = [e for e in events if keep(e)]

c1, c2, c3 = st.columns(3)
dec = [e for e in filt if e["outcome"] != 0]
c1.metric("Trades matched", len(filt))
c2.metric("Win rate (decided)", f"{sum(e['outcome']==1 for e in dec)/len(dec)*100:.0f}%" if dec else "—")
c3.metric("Avg P/L", f"{sum(e['pnl_R'] for e in filt)/len(filt):+.2f}R" if filt else "—")

# --- TAKE-PROFIT CLASSIFIER: how this filtered set does at each target (1R..max) ---
st.subheader("Take-profit comparison (this filtered set)")
LEVELS = sorted(int(k) for k in next(iter(TP.values()))["realized_R"]) if TP else [1, 2, 3, 4, 5]
tp_rows, best_k, best_avg = [], None, -1e9
for k in LEVELS:
    rs = [tp_of(e)["realized_R"][str(k)] for e in filt if tp_of(e)]
    if not rs:
        continue
    avg = sum(rs) / len(rs)
    if avg > best_avg:
        best_avg, best_k = avg, k
    tp_rows.append({"take-profit": f"{k}R",
                    "hit rate": f"{sum(1 for x in rs if x >= k)/len(rs)*100:.0f}%",
                    "avg result (R/trade)": round(avg, 3)})
if tp_rows:
    if best_k is not None:
        st.success(f"📈 Best fixed take-profit for this set: **{best_k}R**  ({best_avg:+.2f}R per trade)")
    ks = [int(r["take-profit"][:-1]) for r in tp_rows]
    avgs = [r["avg result (R/trade)"] for r in tp_rows]
    figb, axb = plt.subplots(figsize=(12, 3.6))
    axb.bar(ks, avgs, color=["#2ca02c" if k == best_k else "#5aa9e6" for k in ks], width=0.7)
    axb.axhline(0, color="gray", lw=0.8)
    axb.set_xticks(ks); axb.set_xticklabels([f"{k}R" for k in ks], fontsize=9)
    axb.set_xlabel("take-profit"); axb.set_ylabel("avg R / trade")
    axb.set_title("Average result per trade by fixed take-profit")
    axb.grid(axis="y", alpha=0.2)
    plt.tight_layout()
    st.pyplot(figb)
    st.table(pd.DataFrame(tp_rows).set_index("take-profit"))

# --- EQUITY CURVE: cumulative R over time, one line per take-profit level ---
st.subheader("Equity curve — cumulative R over the period, per take-profit level")
if filt and TP:
    ec_def = [k for k in (1, 2, 3, 5) if k in LEVELS] or LEVELS[:4]
    ec_levels = st.multiselect("Take-profit levels to plot", LEVELS, ec_def)
    fs = sorted([e for e in filt if tp_of(e)], key=lambda e: e["day"])
    if fs and ec_levels:
        dates = [pd.to_datetime(e["day"]) for e in fs]
        fige, axe = plt.subplots(figsize=(13, 5))
        finals = []
        for k in ec_levels:
            cum, s = [], 0.0
            for e in fs:
                s += tp_of(e)["realized_R"][str(k)]; cum.append(s)
            axe.plot(dates, cum, label=f"{k}R", lw=1.4)
            finals.append(f"**{k}R**: {s:+.0f}R")
        axe.axhline(0, color="gray", lw=0.8)
        axe.set_xlabel("date"); axe.set_ylabel("cumulative R")
        axe.set_title(f"Cumulative R if you took profit at each fixed level  ·  {len(fs)} trades")
        axe.legend(title="take-profit", fontsize=8, ncol=2); axe.grid(alpha=0.2)
        plt.tight_layout()
        st.pyplot(fige)
        st.markdown("**Total over the period** — " + "   ·   ".join(finals))

# --- per-trade table, with the 1R..5R classifier columns ---
def row(e):
    m = tp_of(e); rr = (m or {}).get("realized_R", {})
    return {"symbol": e["symbol"].split(":")[-1], "day": e["day"], "tf": e["timeframe"],
            "catalyst": e.get("reason", ""), "tier": e["size_tier"],
            "entry": round(e["entry_price"], 2), "stop": round(e["stop_price"], 2),
            "risk$": round(e["risk_R"], 3),
            "stop%": round(e["risk_R"] / e["entry_price"] * 100, 3) if e.get("entry_price") else None,
            "MFE_R": round(m["full_mfe_R"], 1) if m else None,
            "@1R": rr.get("1"), "@2R": rr.get("2"), "@3R": rr.get("3"), "@4R": rr.get("4"), "@5R": rr.get("5")}
df = pd.DataFrame([row(e) for e in filt])
st.caption(f"{len(df)} trades shown — click any column header to sort (e.g. **stop%** ascending to surface the tiny-stop rows, or **MFE_R** descending to find the broken R≈0 rows).")
st.dataframe(df, height=520, use_container_width=True)

if filt:
    labels = [f"{e['symbol'].split(':')[-1]} {e['day']} {e['timeframe']}  [{V[e['outcome']]} {e['pnl_R']:+.1f}R]" for e in filt]
    i = st.selectbox("Pick a trade to chart", range(len(filt)), format_func=lambda i: labels[i])
    fig = make_fig(filt[i])
    if fig:
        st.pyplot(fig)
        m = tp_of(filt[i])
        if m:
            rr = m["realized_R"]
            st.info("**Take-profit classifier** — ran to **%.1fR** (MFE)   |   "
                    % m["full_mfe_R"] + "   ".join(f"@{k}R: **{rr[str(k)]:+.1f}R**" for k in [1, 2, 3, 4, 5]))
    else:
        st.warning("No cached chart data for this trade.")
