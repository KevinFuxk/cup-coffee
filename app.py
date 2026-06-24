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
import plotly.graph_objects as go
from collections import defaultdict
from research_data import ResearchData

st.set_page_config(layout="wide", page_title="Cup & Handle Trade Explorer")
V = {1: "WIN", -1: "LOSS", 0: "TIME"}


@st.cache_resource
def get_rd():
    return ResearchData(os.environ["POLYGON_API_KEY"])

@st.cache_data
def load_events(path):
    return [json.loads(l) for l in open(path)]

@st.cache_data
def load_table(path):
    return {r["key"]: r for r in json.load(open(path)) if "key" in r}

@st.cache_data
def load_realprice():
    # {event_key: {real_price, real_risk, rsplits}} from enrich_real_price.py; {} if not built yet
    return json.load(open("data/realprice.json")) if os.path.exists("data/realprice.json") else {}

@st.cache_data
def load_commodity():
    # {ticker: bool} from enrich_commodity.py (SIC-based commodity-sector flag); {} if not built yet
    return json.load(open("data/commodity.json")) if os.path.exists("data/commodity.json") else {}

@st.cache_data
def load_entry_type():
    # {event_key: "momentum"|"consolidation"} from enrich_entry_type.py; {} if not built yet
    return json.load(open("data/entry_type.json")) if os.path.exists("data/entry_type.json") else {}

rd = get_rd()

# --- DATASET SWITCH: which intraday session rule's pile to explore ----------
# This moves the EXITS for the whole page, not just the view:
#   OFF · all-day    -> events.jsonl                 : morning entries ride THROUGH lunch to
#                                                      15:49, and 11:00-13:00 entries are allowed.
#   ON  · lunch rule -> events_WITH_lunch_rule.jsonl : morning entries are flat at 11:00, and
#                                                      NOTHING enters 11:00-13:00 (older pile).
PILES = {
    "OFF · all-day  (hold morning through lunch, trade midday)":
        ("data/events.jsonl", "data/mined_table.json"),
    "ON · lunch rule  (morning flat 11:00, skip 11–1)":
        ("data/events_WITH_lunch_rule.jsonl", "data/mined_table_WITH_lunch_rule.json"),
}
st.sidebar.header("Dataset")
_pile_choice = st.sidebar.radio(
    "Lunch rule", list(PILES.keys()), index=0,
    help="Swaps the WHOLE page between the two backtests — this moves the exits, not just the "
         "view. OFF holds morning trades through lunch to 15:49 and allows 11–1 entries; ON "
         "flattens morning trades at 11:00 and blocks 11–1 entries. Every metric, panel and the "
         "chart picker follow this switch.")
_ev_path, _tab_path = PILES[_pile_choice]
LUNCH_ON = _ev_path.endswith("WITH_lunch_rule.jsonl")

events = load_events(_ev_path)
TP = load_table(_tab_path)
RP = load_realprice()                               # real (un-split-adjusted) price + reverse-split count
COMM = load_commodity()                             # {ticker: True} commodity-sector flag (SIC-based)
HAS_COMMODITY = bool(COMM)
ET = load_entry_type()                              # {event_key: momentum|consolidation}
HAS_ENTRYTYPE = bool(ET)
HAS_SESSION = any(r.get("entry_min") is not None for r in TP.values())
HAS_REALPRICE = bool(RP)

def _k(e):
    return f"{e['symbol']}|{e['day']}|{e['timeframe']}|{e.get('breakout_idx')}|{e.get('handle_num')}"

def tp_of(e):
    return TP.get(_k(e))

def real_price_of(e):                               # un-split-adjusted share price; falls back to adjusted
    r = RP.get(_k(e))
    return r["real_price"] if r else (e.get("entry_price") or 0.0)

def real_risk(e):                                   # dollar stop at the REAL price (for honest cost)
    r = RP.get(_k(e))
    return r["real_risk"] if (r and r.get("real_risk")) else (e.get("risk_R") or 0.0)

def rsplits_of(e):                                  # reverse splits this ticker has ever done (distress flag)
    r = RP.get(_k(e))
    return r.get("rsplits") if r else None

def is_commodity(e):                                # commodity-sector flag (SIC-based), by ticker
    return COMM.get(e["symbol"].split(":")[-1], False)

def entry_type_of(e):                               # "momentum" (flag) | "consolidation" (true cup+handle) | None
    return ET.get(_k(e))


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
_pile_tag = ("🍴 **Lunch rule ON** — morning trades flat at 11:00, no 11–1 entries"
             if LUNCH_ON else
             "🕘 **All-day** — morning trades held through lunch, midday entries allowed")
st.caption(f"{len(events)} detected trades · 5-year backtest (2021–2026) · {_pile_tag} (switch under **Dataset** in the sidebar) · "
           f"Costs charged on **real** (un-split-adjusted) prices · **min real price $15** + min stop 0.25% on by default (the honest view)")

sb = st.sidebar

# ===== ① FILTERS — which trades are in the set (categorical pickers) =====
sb.header("🔎 Filters")
outc = sb.multiselect("Outcome", ["WIN", "LOSS", "TIME"], ["WIN", "LOSS", "TIME"])
reasons = sorted({e.get("reason", "") for e in events})
rsel = sb.multiselect("Catalyst", reasons, reasons)
tfs = sb.multiselect("Timeframe", ["1min", "2min", "5min"], ["1min", "2min", "5min"])
tiers = sorted({e["size_tier"] for e in events})
tsel = sb.multiselect("Size tier", tiers, tiers)
sessions = sb.multiselect("Session (entry time)", ["Morning", "Midday", "Afternoon"], ["Morning", "Midday", "Afternoon"],
                          help="Morning 9:30–11 · Midday 11–1 (the old no-trade lull) · Afternoon 1–3:49")
if not HAS_SESSION:
    sb.caption("⚠️ The lunch-rule pile has no entry-time tags, so this Session filter is inactive "
               "here (all trades pass). It works on the all-day pile.")
symq = sb.text_input("Symbol contains").upper()

# ===== ② SLIDERS — all the dials (quality screens, then costs) =====
sb.header("🎚️ Sliders")
rrng = sb.slider("P/L (R)", -2.0, 20.0, (-2.0, 20.0))
minstop = sb.slider("Min stop (% of price)", 0.0, 3.0, 0.25, 0.05,
                    help="0.25% removes sub-noise tiny-stop patterns (the honest view). Set to 0 to see the raw pile, incl. broken R≈0 rows that inflate the high take-profits.")
minprice = sb.slider("Min REAL price ($)", 0.0, 50.0, 15.0, 1.0,
                    help="Screens on each stock's REAL (un-split-adjusted) price. Defaults to $15 — reverse-split "
                         "ghosts (e.g. DBGI shown at $678k but really ~$6) and other sub-$15 penny names are dropped "
                         "for good: their fixed ¢/share costs would be brutal and they're low-quality. Set 0 to include them. "
                         "(Needs data/realprice.json from enrich_real_price.py.)")
if not HAS_REALPRICE:
    sb.caption("⚠️ data/realprice.json not found — real-price screen & honest costs are OFF "
               "(falling back to adjusted prices). Run `python enrich_real_price.py`.")
sb.caption("**Costs** — subtracted from every R")
commission = sb.slider("Commission (¢/share, each way)", 0.0, 1.0, 0.3, 0.05,
                       help="Per-share broker fee, charged on BOTH the buy and the sell. STG / day-trading brokers ≈ 0.2–0.5¢.")
slippage = sb.slider("Entry slippage (¢)", 0.0, 10.0, 2.0, 0.5,
                     help="Cents worse than your trigger on the entry fill. Exit ≈ 0 (take-profit = limit, stop = stop-market). A fixed few ¢ is a BIG fraction of a tiny stop.")

# ===== ③ COMPARE TOGGLES — buttons, in the SAME order as the compare panels below =====
sb.header("🔘 Compare toggles")
excl_commodity = sb.checkbox("Exclude commodity stocks", value=False,
                    help="Drops oil/gas, metals/mining, coal, ag & primary-metals names (SIC-based) from every "
                         "panel EXCEPT its compare panel. Default OFF — read the 'Commodity stocks' compare panel "
                         "first. (Needs data/commodity.json from enrich_commodity.py.)")
if not HAS_COMMODITY:
    sb.caption("⚠️ data/commodity.json not found — run `python enrich_commodity.py`.")
cup_only = sb.checkbox("Cup-and-handle only (drop momentum/flag entries)", value=False,
                    help="Drops momentum-path entries (fast re-break, no handle — the 'high tight flag' case), "
                         "keeping only true cup-and-handle (consolidation) entries. Read the 'Entry type' compare "
                         "panel first. (Needs data/entry_type.json from enrich_entry_type.py.)")
if not HAS_ENTRYTYPE:
    sb.caption("⚠️ data/entry_type.json not found — run `python enrich_entry_type.py`.")

def cost_in_R(e):
    # entry slippage (1 side) + commission round-trip (2 sides), in $, divided by the REAL dollar
    # risk (un-split-adjusted) — so reverse-split ghosts are costed at their true penny prices.
    rr = real_risk(e)
    return ((slippage + 2 * commission) / 100.0) / rr if rr > 0 else 0.0

def net_at(e, k):                                   # cost-adjusted realized R at take-profit k
    m = tp_of(e)
    if not m or str(k) not in m["realized_R"]:
        return None
    return m["realized_R"][str(k)] - cost_in_R(e)

def curve_stats(rows, k):                           # (max drawdown R, annualized Sharpe) of net R at take-profit k
    if not k:
        return None, None
    pairs = sorted((e["day"], net_at(e, k)) for e in rows if net_at(e, k) is not None)
    if not pairs:
        return None, None
    run = mdd = 0.0; peak = -1e18                    # max drawdown of the date-ordered cumulative R
    for _, r in pairs:
        run += r; peak = max(peak, run); mdd = max(mdd, peak - run)
    daily = defaultdict(float)                       # sum net R per trading day -> the "daily return" (in R)
    for d, r in pairs:
        daily[d] += r
    ds = list(daily.values())
    sharpe = None
    if len(ds) > 1:
        mean = sum(ds) / len(ds)
        sd = (sum((x - mean) ** 2 for x in ds) / len(ds)) ** 0.5
        sharpe = (mean / sd) * (252 ** 0.5) if sd > 1e-9 else None   # annualize from daily
    return mdd, sharpe

def session_of(e):                                  # Morning / Midday / Afternoon from entry-time tag
    m = tp_of(e)
    em = m.get("entry_min") if m else None
    if em is None:
        return None
    return "Morning" if em < 660 else ("Midday" if em < 780 else "Afternoon")


def keep(e):
    sp = (e["risk_R"] / e["entry_price"] * 100) if e.get("entry_price") else 0.0
    se = session_of(e)
    return (V[e["outcome"]] in outc and e.get("reason", "") in rsel and e["timeframe"] in tfs
            and e["size_tier"] in tsel and symq in e["symbol"].upper()
            and rrng[0] <= e["pnl_R"] <= rrng[1] and sp >= minstop
            and real_price_of(e) >= minprice
            and (se in sessions or se is None))

filt_all = [e for e in events if keep(e)]           # before the toggles (the compare panels use this)
filt = [e for e in filt_all
        if not (excl_commodity and is_commodity(e))
        and not (cup_only and entry_type_of(e) == "momentum")]

LEVELS = sorted(int(k) for k in next(iter(TP.values()))["realized_R"]) if TP else [1, 2, 3, 4, 5]
best_k, best_avg = None, -1e9                        # best fixed take-profit (net of costs) for this set
for k in LEVELS:
    vals = [net_at(e, k) for e in filt if net_at(e, k) is not None]
    if vals:
        a = sum(vals) / len(vals)
        if a > best_avg:
            best_avg, best_k = a, k

mdd_best, sharpe_best = curve_stats(filt, best_k)
c1, c2, c3, c4, c5 = st.columns(5)
dec = [e for e in filt if e["outcome"] != 0]
c1.metric("Trades matched", len(filt))
c2.metric("Win rate (decided)", f"{sum(e['outcome']==1 for e in dec)/len(dec)*100:.0f}%" if dec else "—")
c3.metric("Best take-profit (net)", f"{best_avg:+.2f}R @ {best_k}R" if best_k else "—")
c4.metric("Max drawdown", f"{mdd_best:.0f}R" if mdd_best is not None else "—",
          help=f"Worst peak-to-trough dip of the cumulative net-R curve at the best take-profit"
               f"{f' ({best_k}R)' if best_k else ''}. Lower = less risk. Reflects the current Filters, Sliders & toggles.")
c5.metric("Sharpe (annual)", f"{sharpe_best:.2f}" if sharpe_best is not None else "—",
          help="Annualized Sharpe at the best take-profit: sum net R per trading day, take mean ÷ std, "
               "× √252. >1 decent, >2 good. Based on active trading days; reflects the current Filters, Sliders & toggles.")

# --- TAKE-PROFIT CLASSIFIER: how this filtered set does at each target (1R..max) ---
st.subheader("Take-profit comparison (this filtered set)")
st.caption(f"Average R is **net of costs** ({slippage:.1f}¢ entry slippage + {commission:.2f}¢/share each-way commission, set in the sidebar); hit-rate is gross.")
tp_rows = []
for k in LEVELS:
    ev = [e for e in filt if tp_of(e)]
    if not ev:
        continue
    raws = [tp_of(e)["realized_R"][str(k)] for e in ev]          # gross (did it reach k)
    avg = sum(net_at(e, k) for e in ev) / len(ev)                # NET of costs
    tp_rows.append({"take-profit": f"{k}R",
                    "hit rate": f"{sum(1 for x in raws if x >= k)/len(raws)*100:.0f}%",
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
        fige = go.Figure()
        finals = []
        for k in ec_levels:
            cum, s = [], 0.0
            for e in fs:
                s += net_at(e, k); cum.append(s)                 # cost-adjusted
            fige.add_trace(go.Scatter(x=dates, y=cum, mode="lines", name=f"{k}R"))
            finals.append(f"**{k}R**: {s:+.0f}R")
        fige.add_hline(y=0, line_color="gray", line_width=1)
        fige.update_layout(height=460, xaxis_title="date", yaxis_title="cumulative R (net of costs)",
                           title=f"Cumulative R if you took profit at each fixed level · {len(fs)} trades",
                           legend_title="take-profit", hovermode="x unified",
                           margin=dict(l=50, r=20, t=50, b=40))
        st.plotly_chart(fige, use_container_width=True)
        st.markdown("**Total over the period** — " + "   ·   ".join(finals))

# --- COMMODITY COMPARE: include vs exclude commodity-sector names (return + risk) ---
st.subheader("Commodity stocks — include vs exclude (return & risk)")
if HAS_COMMODITY and filt_all and TP:
    cmp_k = st.selectbox("Compare at take-profit", LEVELS,
                         index=LEVELS.index(best_k) if best_k in LEVELS else 0, key="cmp_tp")

    def _mdd(cum):                                   # max drawdown of a cumulative-R curve
        peak, mdd = -1e18, 0.0
        for x in cum:
            peak = max(peak, x); mdd = max(mdd, peak - x)
        return mdd

    def _stats(rows):
        vals = [(e, net_at(e, cmp_k)) for e in rows if net_at(e, cmp_k) is not None]
        if not vals:
            return None
        svals = sorted(vals, key=lambda ev: ev[0]["day"])
        cum, s = [], 0.0
        for _, r in svals:
            s += r; cum.append(s)
        n, mdd = len(vals), _mdd(cum)
        return {"trades": n, "total_R": s, "rpt": s / n,
                "win": 100 * sum(1 for _, r in vals if r > 0) / n,
                "mdd": mdd, "calmar": (s / mdd) if mdd > 1e-9 else float("inf"),
                "cum": cum, "dates": [pd.to_datetime(e["day"]) for e, _ in svals]}

    inc = _stats(filt_all)
    exc = _stats([e for e in filt_all if not is_commodity(e)])
    only = _stats([e for e in filt_all if is_commodity(e)])
    if inc and exc:
        def _fmt(d):
            return {"trades": d["trades"], "total R": round(d["total_R"]),
                    "R/trade": round(d["rpt"], 3), "win %": round(d["win"]),
                    "max drawdown (R)": round(d["mdd"]),
                    "Calmar (R÷DD)": ("∞" if d["calmar"] == float("inf") else round(d["calmar"], 2))}
        cols = {"Include commodity": _fmt(inc), "Exclude commodity": _fmt(exc)}
        if only:
            cols["Commodity only"] = _fmt(only)
        st.table(pd.DataFrame(cols))
        figc = go.Figure()
        figc.add_trace(go.Scatter(x=inc["dates"], y=inc["cum"], mode="lines", name="Include commodity"))
        figc.add_trace(go.Scatter(x=exc["dates"], y=exc["cum"], mode="lines", name="Exclude commodity"))
        figc.add_hline(y=0, line_color="gray", line_width=1)
        figc.update_layout(height=420, xaxis_title="date", yaxis_title=f"cumulative net R @ {cmp_k}R",
                           title=f"Equity curve — include vs exclude commodity names (net, @ {cmp_k}R)",
                           hovermode="x unified", margin=dict(l=50, r=20, t=50, b=40))
        st.plotly_chart(figc, use_container_width=True)
        n_comm = only["trades"] if only else 0
        st.caption(f"Excluding the **{n_comm}** commodity trades changes total by **{exc['total_R']-inc['total_R']:+.0f}R** "
                   f"and max drawdown by **{exc['mdd']-inc['mdd']:+.0f}R** (lower drawdown = less risk). "
                   "Decide from the numbers, then tick **Exclude commodity stocks** in the sidebar to apply it everywhere.")
elif not HAS_COMMODITY:
    st.info("Run `python enrich_commodity.py` to build data/commodity.json — then this compare panel appears.")

# --- ENTRY-TYPE COMPARE: true cup-and-handle (consolidation) vs momentum (flag) ---
st.subheader("Entry type — true cup-and-handle vs momentum (flag) entries")
if HAS_ENTRYTYPE and filt_all and TP:
    etk = st.selectbox("Compare at take-profit", LEVELS,
                       index=LEVELS.index(best_k) if best_k in LEVELS else 0, key="et_tp")

    def _dd2(cum):
        peak, m = -1e18, 0.0
        for x in cum:
            peak = max(peak, x); m = max(m, peak - x)
        return m

    def _st2(rows):
        vals = [(e, net_at(e, etk)) for e in rows if net_at(e, etk) is not None]
        if not vals:
            return None
        sv = sorted(vals, key=lambda ev: ev[0]["day"])
        cum, s = [], 0.0
        for _, r in sv:
            s += r; cum.append(s)
        n, d = len(vals), _dd2(cum)
        return {"trades": n, "total_R": s, "rpt": s / n,
                "win": 100 * sum(1 for _, r in vals if r > 0) / n,
                "dd": d, "calmar": (s / d) if d > 1e-9 else float("inf"),
                "cum": cum, "dates": [pd.to_datetime(e["day"]) for e, _ in sv]}

    allr = _st2(filt_all)
    cons = _st2([e for e in filt_all if entry_type_of(e) == "consolidation"])
    momo = _st2([e for e in filt_all if entry_type_of(e) == "momentum"])
    if allr and cons:
        def _f(d):
            return {"trades": d["trades"], "total R": round(d["total_R"]),
                    "R/trade": round(d["rpt"], 3), "win %": round(d["win"]),
                    "max drawdown (R)": round(d["dd"]),
                    "Calmar (R÷DD)": ("∞" if d["calmar"] == float("inf") else round(d["calmar"], 2))}
        cols = {"All entries": _f(allr), "Cup-and-handle only": _f(cons)}
        if momo:
            cols["Momentum (flag) only"] = _f(momo)
        st.table(pd.DataFrame(cols))
        figt = go.Figure()
        figt.add_trace(go.Scatter(x=allr["dates"], y=allr["cum"], mode="lines", name="All entries"))
        figt.add_trace(go.Scatter(x=cons["dates"], y=cons["cum"], mode="lines", name="Cup-and-handle only"))
        figt.add_hline(y=0, line_color="gray", line_width=1)
        figt.update_layout(height=420, xaxis_title="date", yaxis_title=f"cumulative net R @ {etk}R",
                           title=f"Equity curve — all entries vs cup-and-handle only (drop momentum/flag), @ {etk}R",
                           hovermode="x unified", margin=dict(l=50, r=20, t=50, b=40))
        st.plotly_chart(figt, use_container_width=True)
        nmom = momo["trades"] if momo else 0
        st.caption(f"Dropping the **{nmom}** momentum/flag entries changes total by "
                   f"**{cons['total_R']-allr['total_R']:+.0f}R** and max drawdown by **{cons['dd']-allr['dd']:+.0f}R** "
                   "(lower = less risk). Tick **Cup-and-handle only** in the sidebar to apply it everywhere.")
elif not HAS_ENTRYTYPE:
    st.info("Run `python enrich_entry_type.py` to build data/entry_type.json — then this compare panel appears.")

# --- LOOSE vs STRICT CUP: two independent piles, head to head ---
st.subheader("Loose vs Strict cup — the two backtests, head to head")
@st.cache_data
def load_pile(ev_path, tab_path, rp_path):
    if not (os.path.exists(ev_path) and os.path.exists(tab_path) and os.path.exists(rp_path)):
        return None
    evl = [json.loads(l) for l in open(ev_path) if l.strip()]
    tabl = {r["key"]: r for r in json.load(open(tab_path)) if "key" in r}
    return evl, tabl, json.load(open(rp_path))
_strict_pile = load_pile("data/events_strict_cup.jsonl", "data/mined_table_strict_cup.json", "data/realprice_strict_cup.json")
_loose_pile  = load_pile("data/events_loose_cup.jsonl", "data/mined_table_loose_cup.json", "data/realprice_loose_cup.json")
if _strict_pile and _loose_pile:
    lvk = st.selectbox("Compare at take-profit", LEVELS,
                       index=LEVELS.index(best_k) if best_k in LEVELS else 0, key="lv_tp")
    _FEE = (slippage + 2 * commission) / 100.0
    def _pile_verdict(pile, k):
        evl, tabl, rpl = pile
        rows = []
        for e in evl:
            kk = f'{e["symbol"]}|{e["day"]}|{e["timeframe"]}|{e.get("breakout_idx")}|{e.get("handle_num")}'
            m = tabl.get(kk)
            if not m or str(k) not in m["realized_R"]:
                continue
            r = rpl.get(kk, {})
            rprice = r.get("real_price", e.get("entry_price") or 0)
            rrisk = r.get("real_risk") or e.get("risk_R") or 0
            stoppct = (e["risk_R"] / e["entry_price"] * 100) if e.get("entry_price") else 0
            if rprice < minprice or stoppct < minstop:        # SAME screen as the sidebar sliders
                continue
            rows.append((e["day"], m["realized_R"][str(k)] - (_FEE / rrisk if rrisk > 0 else 0)))
        if not rows:
            return None
        rows.sort()
        cum, s = [], 0.0
        for _, nr in rows:
            s += nr; cum.append(s)
        mdd = 0.0; peak = -1e18
        for c in cum:
            peak = max(peak, c); mdd = max(mdd, peak - c)
        daily = defaultdict(float)
        for d, nr in rows:
            daily[d] += nr
        ds = list(daily.values()); mn = sum(ds) / len(ds)
        sd = (sum((x - mn) ** 2 for x in ds) / len(ds)) ** 0.5
        n = len(rows)
        return {"trades": n, "total": s, "rpt": s / n, "win": 100 * sum(1 for _, nr in rows if nr > 0) / n,
                "mdd": mdd, "sharpe": (mn / sd * 252 ** 0.5 if sd > 1e-9 else 0),
                "calmar": (s / mdd if mdd > 1e-9 else float("inf")),
                "cum": cum, "dates": [pd.to_datetime(d) for d, _ in rows]}
    lv = _pile_verdict(_loose_pile, lvk)
    sv = _pile_verdict(_strict_pile, lvk)
    if lv and sv:
        def _f(d):
            return {"trades": d["trades"], "total R": round(d["total"]), "R/trade": round(d["rpt"], 3),
                    "win %": round(d["win"]), "max drawdown (R)": round(d["mdd"]),
                    "Sharpe (ann)": round(d["sharpe"], 2),
                    "Calmar": ("∞" if d["calmar"] == float("inf") else round(d["calmar"], 2))}
        st.table(pd.DataFrame({"Loose cup": _f(lv), "Strict cup": _f(sv)}))
        figlv = go.Figure()
        figlv.add_trace(go.Scatter(x=lv["dates"], y=lv["cum"], mode="lines", name="Loose cup"))
        figlv.add_trace(go.Scatter(x=sv["dates"], y=sv["cum"], mode="lines", name="Strict cup"))
        figlv.add_hline(y=0, line_color="gray", line_width=1)
        figlv.update_layout(height=440, xaxis_title="date", yaxis_title=f"cumulative net R @ {lvk}R",
                            title=f"Loose vs Strict cup — two independent backtests, net @ {lvk}R (your screen + cost settings)",
                            hovermode="x unified", margin=dict(l=50, r=20, t=50, b=40))
        st.plotly_chart(figlv, use_container_width=True)
        st.caption("Two INDEPENDENT piles, both run through your sidebar min-stop / min-price / cost settings: "
                   "**Loose** = the pre-strict pile (`events_loose_cup.jsonl`), **Strict** = the current re-run "
                   "(`events.jsonl`, max→min rim symmetry). Move the take-profit selector to compare at any level.")
else:
    st.info("Loose vs Strict needs both piles. Loose = data/*_loose_cup.* — if missing, run: "
            "`python enrich_real_price.py data/events_loose_cup.jsonl data/realprice_loose_cup.json`.")

# --- PER-YEAR PERFORMANCE across take-profit levels (interactive) ---
st.subheader("Performance by year — across take-profit levels")
if filt and TP:
    by_year = defaultdict(list)
    for e in filt:
        if tp_of(e):
            by_year[e["day"][:4]].append(e)
    years = sorted(by_year)
    if years:
        figy = go.Figure()
        for y in years:
            ev = by_year[y]
            ys = []
            for k in LEVELS:
                vals = [net_at(e, k) for e in ev if net_at(e, k) is not None]
                ys.append(round(sum(vals) / len(vals), 4) if vals else None)
            figy.add_trace(go.Scatter(x=LEVELS, y=ys, mode="lines+markers", name=f"{y}  ({len(ev)})"))
        figy.add_hline(y=0, line_color="gray", line_width=1)
        figy.update_layout(height=470, xaxis_title="take-profit (R)", yaxis_title="net R / trade",
                           title="Net R per trade by year, across take-profit levels (after costs)",
                           legend_title="year (trades)", hovermode="x unified",
                           margin=dict(l=50, r=20, t=50, b=40))
        st.plotly_chart(figy, use_container_width=True)
        st.caption("Each line is one year's take-profit curve — see where holding for more R pays off vs fades, year by year. "
                   "Hover for exact values; click a year in the legend to toggle it. Net of costs · respects all filters.")

# --- PERFORMANCE BY SESSION (time of day) ---
st.subheader("Performance by session — Morning / Midday / Afternoon")
if not HAS_SESSION:
    st.info("The **lunch-rule** pile predates entry-time tagging, so the time-of-day split isn't "
            "available here. Flip the **Dataset** toggle to **all-day** to see Morning / Midday / "
            "Afternoon. (The lunch-rule pile has no midday trades by construction anyway.)")
elif filt and TP:
    bysess = defaultdict(list)
    for e in filt:
        s = session_of(e)
        if s:
            bysess[s].append(e)
    labels = {"Morning": "Morning (9:30–11)", "Midday": "Midday (11–1, the lull)", "Afternoon": "Afternoon (1–3:49)"}
    figs = go.Figure()
    for s in ["Morning", "Midday", "Afternoon"]:
        if s not in bysess:
            continue
        ev = bysess[s]
        ys = []
        for k in LEVELS:
            vals = [net_at(e, k) for e in ev if net_at(e, k) is not None]
            ys.append(round(sum(vals) / len(vals), 4) if vals else None)
        figs.add_trace(go.Scatter(x=LEVELS, y=ys, mode="lines+markers", name=f"{labels[s]}  ({len(ev)})"))
    figs.add_hline(y=0, line_color="gray", line_width=1)
    figs.update_layout(height=440, xaxis_title="take-profit (R)", yaxis_title="net R / trade",
                       title="Net R per trade by time-of-day session, across take-profit (after costs)",
                       legend_title="session (trades)", hovermode="x unified", margin=dict(l=50, r=20, t=50, b=40))
    st.plotly_chart(figs, use_container_width=True)
    st.caption("The **Midday (11–1)** line is the lunch trades you used to skip — positive, but a notch below the others. "
               "Use the sidebar **Session** filter to isolate any one across every panel.")

# --- QQQ-ONLY take-profit panel (standalone: ALL QQQ trades; only the cost sliders apply) ---
st.subheader("QQQ only — take-profit results")
qqq = [e for e in events if "QQQ" in e["symbol"] and tp_of(e)]
if qqq:
    net_avg, gross_avg = [], []
    for k in LEVELS:
        nv = [net_at(e, k) for e in qqq if net_at(e, k) is not None]
        gv = [tp_of(e)["realized_R"][str(k)] for e in qqq]
        net_avg.append(round(sum(nv) / len(nv), 4) if nv else None)
        gross_avg.append(round(sum(gv) / len(gv), 4) if gv else None)
    figq = go.Figure()
    figq.add_trace(go.Bar(x=LEVELS, y=net_avg, name="net of costs",
                          marker_color=["#2ca02c" if (v or 0) >= 0 else "#c01c28" for v in net_avg],
                          hovertemplate="%{x}R · net %{y:+.3f}R<extra></extra>"))
    figq.add_trace(go.Scatter(x=LEVELS, y=gross_avg, name="gross (no costs)", mode="lines+markers",
                              line=dict(color="#888", dash="dot"),
                              hovertemplate="%{x}R · gross %{y:+.3f}R<extra></extra>"))
    figq.add_hline(y=0, line_color="gray", line_width=1)
    figq.update_layout(height=430, xaxis_title="take-profit (R)", yaxis_title="avg R / trade",
                       title=f"QQQ only · {len(qqq)} trades · net (bars) vs gross (dotted)",
                       margin=dict(l=50, r=20, t=50, b=40),
                       xaxis=dict(tickmode="array", tickvals=LEVELS, ticktext=[f"{k}R" for k in LEVELS]))
    st.plotly_chart(figq, use_container_width=True)
    st.caption("Standalone: **all QQQ trades** (ignores the sidebar filters) but the **commission + slippage sliders apply**. "
               "⚠️ QQQ is the index reference; its stops are tiny (median ~0.05% of price, all below the 0.25% floor you'd normally trade), "
               "so these R-multiples ride sub-noise stops — in reality slippage would bite harder than the slider shows.")
    st.markdown("**QQQ by year** — same take-profit curves, split by year:")
    qby = defaultdict(list)
    for e in qqq:
        qby[e["day"][:4]].append(e)
    if qby:
        figqy = go.Figure()
        for y in sorted(qby):
            ev = qby[y]
            ys = []
            for k in LEVELS:
                vals = [net_at(e, k) for e in ev if net_at(e, k) is not None]
                ys.append(round(sum(vals) / len(vals), 4) if vals else None)
            figqy.add_trace(go.Scatter(x=LEVELS, y=ys, mode="lines+markers", name=f"{y}  ({len(ev)})"))
        figqy.add_hline(y=0, line_color="gray", line_width=1)
        figqy.update_layout(height=430, xaxis_title="take-profit (R)", yaxis_title="net R / trade",
                            title="QQQ net R/trade by year, across take-profit levels (after costs)",
                            legend_title="year (trades)", hovermode="x unified",
                            margin=dict(l=50, r=20, t=50, b=40))
        st.plotly_chart(figqy, use_container_width=True)

# --- per-trade table, with the 1R..5R classifier columns ---
def row(e):
    m = tp_of(e); rr = (m or {}).get("realized_R", {})
    return {"symbol": e["symbol"].split(":")[-1], "day": e["day"], "tf": e["timeframe"],
            "catalyst": e.get("reason", ""), "tier": e["size_tier"],
            "entry": round(e["entry_price"], 2), "real$": round(real_price_of(e), 2),
            "rev_splits": rsplits_of(e), "stop": round(e["stop_price"], 2),
            "risk$": round(e["risk_R"], 3),
            "stop%": round(e["risk_R"] / e["entry_price"] * 100, 3) if e.get("entry_price") else None,
            "MFE_R": round(m["full_mfe_R"], 1) if m else None,
            "@1R": (round(net_at(e, 1), 2) if net_at(e, 1) is not None else None),
            "@2R": (round(net_at(e, 2), 2) if net_at(e, 2) is not None else None),
            "@3R": (round(net_at(e, 3), 2) if net_at(e, 3) is not None else None),
            "@4R": (round(net_at(e, 4), 2) if net_at(e, 4) is not None else None),
            "@5R": (round(net_at(e, 5), 2) if net_at(e, 5) is not None else None)}
df = pd.DataFrame([row(e) for e in filt])
st.caption(f"{len(df)} trades shown — click any header to sort. **real$** = un-split-adjusted price "
           "(sort ascending to see what the $15 screen removes); **rev_splits** = reverse splits the ticker ever did "
           "(distress flag, not filtered); **stop%** ascending surfaces tiny-stop rows.")
st.dataframe(df, height=520, use_container_width=True)

if filt:
    labels = [f"{e['symbol'].split(':')[-1]} {e['day']} {e['timeframe']}  [{V[e['outcome']]} {e['pnl_R']:+.1f}R]" for e in filt]
    i = st.selectbox("Pick a trade to chart", range(len(filt)), format_func=lambda i: labels[i])
    fig = make_fig(filt[i])
    if fig:
        st.pyplot(fig)
        ei = filt[i]; m = tp_of(ei)
        if m:
            st.info("**Take-profit classifier (net of costs)** — ran to **%.1fR** (MFE)   |   "
                    % m["full_mfe_R"] + "   ".join(f"@{k}R: **{net_at(ei, k):+.1f}R**" for k in [1, 2, 3, 4, 5]))
    else:
        st.warning("No cached chart data for this trade.")
