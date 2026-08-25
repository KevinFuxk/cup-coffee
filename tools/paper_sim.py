"""
paper_sim.py — paper-trade EXECUTION + SIZING simulator (concurrency-aware)
===========================================================================
Replays the strategy's signals through the live order model (bracket OCO exit) + your sizing,
in DOLLARS, with REALISTIC CONCURRENCY: each trade holds an intraday open->close interval, and a
new signal is TAKEN only if (a) a position slot is free AND (b) buying power allows (intraday
leverage cap). Signals that don't fit are SKIPPED — exactly as a real, finite account would.

Sizing: R$ = risk_pct of CURRENT (realized) equity; shares = R$ / per-share-stop; a position's
notional consumes buying power (leverage * equity) while it's open. Exit: OCO stop+TP; flat 15:49.

Two numbers printed: UNCONSTRAINED (take every signal — the fantasy) vs CONSTRAINED (real cap +
buying power). The gap is the cost of having a finite account. Reads only local files; no network.
    python paper_sim.py --cash 1000 --risk 0.01 --tp 6 --max_positions 10 --leverage 4
"""
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)
import json, argparse
from collections import defaultdict

BARMIN = {"1min": 1, "2min": 2, "5min": 5}


def key(e):
    return f'{e["symbol"]}|{e["day"]}|{e["timeframe"]}|{e.get("breakout_idx")}|{e.get("handle_num")}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cash", type=float, default=1000.0)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--tp", type=int, default=6)
    ap.add_argument("--slip", type=float, default=2.0)
    ap.add_argument("--comm", type=float, default=0.3)
    ap.add_argument("--minprice", type=float, default=15.0)
    ap.add_argument("--minstop", type=float, default=0.25)
    ap.add_argument("--max_positions", type=int, default=10)
    ap.add_argument("--leverage", type=float, default=4.0)    # intraday margin (PDT account)
    ap.add_argument("--fixed_r", type=float, default=0.0)     # fixed $ risk/trade; 0 = use --risk % of equity
    ap.add_argument("--daily_loss_limit", type=float, default=0.0)  # halt new entries once a day's realized P&L <= -x*day-start equity; 0=off
    a = ap.parse_args()
    FEE = (a.slip + 2 * a.comm) / 100.0

    tab = {r["key"]: r for r in json.load(open("data/mined_table.json")) if "key" in r}
    RP = json.load(open("data/realprice.json"))
    evs = [json.loads(l) for l in open("data/events.jsonl") if l.strip()]
    realp = lambda e: RP.get(key(e), {}).get("real_price", e.get("entry_price") or 0)
    realr = lambda e: RP.get(key(e), {}).get("real_risk") or e["risk_R"]
    stoppct = lambda e: (e["risk_R"] / e["entry_price"] * 100) if e.get("entry_price") else 0

    # build trades with an intraday open->close interval (exit proxied by the labeled hold length)
    trades = []
    for e in evs:
        if key(e) not in tab or realp(e) < a.minprice or stoppct(e) < a.minstop:
            continue
        m = tab[key(e)]; em = m.get("entry_min")
        if em is None:
            continue
        held = max(0, (e.get("exit_idx", 0) - e.get("breakout_idx", 0))) * BARMIN.get(e["timeframe"], 1)
        rr = realr(e); net = m["realized_R"][str(a.tp)] - (FEE / rr if rr > 0 else 0)
        trades.append({"day": e["day"], "em": em, "xm": em + held, "net": net,
                       "rr": rr, "price": realp(e), "sym": e["symbol"].split(":")[-1], "tf": e["timeframe"]})
    trades.sort(key=lambda t: (t["day"], t["em"]))

    # --- UNCONSTRAINED: take every signal, compound sequentially (the fantasy) ---
    eq = a.cash
    for t in trades:
        eq += t["net"] * (a.fixed_r if a.fixed_r > 0 else a.risk * eq)
    unconstrained = eq

    # --- CONSTRAINED: event-driven, position cap + buying-power cap + daily-loss circuit-breaker ---
    equity = peak = a.cash
    maxdd = max_lev = 0.0
    open_pos = []                                            # {exit_key, day, notional, net, Rd}
    taken = skip_cap = skip_bp = skip_daily = 0; max_conc = 0; shown = 0
    day_start_eq = {}; day_pnl = defaultdict(float); days_halted = set()

    def close_due(now_key):
        nonlocal equity, peak, maxdd, open_pos
        keep = []
        for p in sorted(open_pos, key=lambda p: p["exit_key"]):
            if p["exit_key"] <= now_key:
                pnl = p["net"] * p["Rd"]
                equity += pnl; day_pnl[p["day"]] += pnl       # attribute P&L to the day the position exited
                peak = max(peak, equity); maxdd = max(maxdd, (peak - equity) / peak)
            else:
                keep.append(p)
        open_pos = keep

    for t in trades:
        now = (t["day"], t["em"])
        close_due(now)                                       # realize exits that happened before this entry
        day_start_eq.setdefault(t["day"], equity)            # clean start-of-day equity (prior days now closed)
        Rd = a.fixed_r if a.fixed_r > 0 else a.risk * equity
        notional = (Rd / t["rr"]) * t["price"] if t["rr"] > 0 else 0
        open_notional = sum(p["notional"] for p in open_pos)
        if a.daily_loss_limit > 0 and day_pnl[t["day"]] <= -a.daily_loss_limit * day_start_eq[t["day"]]:
            skip_daily += 1; days_halted.add(t["day"]); decision = "SKIP(daily-loss-limit)"
        elif len(open_pos) >= a.max_positions:
            skip_cap += 1; decision = "SKIP(cap)"
        elif open_notional + notional > a.leverage * equity:
            skip_bp += 1; decision = "SKIP(buying-power)"
        else:
            open_pos.append({"exit_key": (t["day"], t["xm"]), "day": t["day"],
                             "notional": notional, "net": t["net"], "Rd": Rd})
            taken += 1; max_conc = max(max_conc, len(open_pos))
            max_lev = max(max_lev, (open_notional + notional) / equity); decision = "OPEN"
        if shown < 8:
            print(f"  {t['day']} {t['sym']:6} {t['tf']:4} ${notional:>7.0f} "
                  f"open={len(open_pos)} -> {decision:22} eq ${equity:,.0f}")
            shown += 1
    close_due(("9999", 99999))

    rmode = f"R=${a.fixed_r:.0f} fixed" if a.fixed_r > 0 else f"risk {a.risk*100:.2f}%"
    print(f"\nsignals {len(trades)} | start ${a.cash:,.0f} | {rmode} | TP {a.tp}R | "
          f"cap {a.max_positions} pos | {a.leverage:.0f}:1 margin\n")
    print(f"UNCONSTRAINED (take every signal):  ${unconstrained:,.0f}")
    print(f"CONSTRAINED  (real cap + margin):   ${equity:,.0f}   <- the realistic number")
    dl = f" + {skip_daily} (daily-loss-limit, {len(days_halted)} days halted)" if a.daily_loss_limit > 0 else ""
    print(f"  taken {taken} / {len(trades)}   skipped: {skip_cap} (slot cap) + {skip_bp} (buying power){dl}")
    print(f"  max concurrent positions {max_conc}   |   peak leverage {max_lev:.1f}x   |   max drawdown {maxdd*100:.0f}%")


if __name__ == "__main__":
    main()
