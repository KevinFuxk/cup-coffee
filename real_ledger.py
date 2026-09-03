"""
real_ledger.py — the ARMED trades, reconstructed from IBKR's own executions
==========================================================================
The replay ledger records what the rules SAID the day was worth. This records what
the market actually GAVE us: real entry/exit fills (partials averaged), the exit kind
from the bracket leg that fired, R against the bracket's own trigger-stop, and the
highest R the trade reached (peak_R, from the cached 15s bars) — so real and replay
sit side by side in the same ledger, variant "armed".

Sources, all point-in-time:
  * IBKR executions (reqExecutions serves TODAY only -> saved to data/executions/<day>.csv
    the first time, then read from disk forever)
  * logs/live_<day>.log for each bracket's trigger/stop (the 🛡️ and 🔧 lines)
  * data/paper_fills.csv for which bracket leg fired (TP / SL)
  * cache/ibkr15s/<SYM>/<day>.json for the path between entry and exit (pulled if missing)

EOD closes are plain market orders (no orderRef): they are matched to open bracket
entries FIFO by symbol. Anything left over is reported as "churn" — the 2026-09-02
flatten storm made that category real.

    python real_ledger.py 2026-09-02
"""
from __future__ import annotations

import csv
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from record_day import append_ledger, bars_15s

ET = ZoneInfo("America/New_York")
EXEC_DIR = "data/executions"


def load_executions(day: str) -> list[dict]:
    path = f"{EXEC_DIR}/{day}.csv"
    if os.path.exists(path):
        return list(csv.DictReader(open(path)))
    if day != datetime.now(ET).date().isoformat():
        sys.exit(f"✗ IBKR only serves today's executions and {path} was never saved")
    from ib_async import IB, ExecutionFilter
    ib = IB(); ib.connect("127.0.0.1", 4002, clientId=54, timeout=10)
    fills = ib.reqExecutions(ExecutionFilter()); ib.sleep(2)
    ib.disconnect()
    rows = []
    for f in sorted(fills, key=lambda f: f.execution.time):
        e = f.execution
        t = e.time.astimezone(ET)
        if t.date().isoformat() != day:
            continue
        rows.append({"time": t.strftime("%H:%M:%S"), "symbol": f.contract.symbol, "side": e.side,
                     "shares": f"{e.shares:.0f}", "price": f"{e.price:.4f}",
                     "ref": e.orderRef or "", "exec_id": e.execId})
    os.makedirs(EXEC_DIR, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["time", "symbol", "side", "shares", "price", "ref", "exec_id"])
        w.writeheader(); w.writerows(rows)
    print(f"  saved {len(rows)} executions -> {path}")
    return rows


def arm_levels(day: str) -> dict[str, list[tuple[dtime, float, float]]]:
    """symbol -> [(time, trigger, stop)] from the day's live log, chronological."""
    out: dict[str, list] = defaultdict(list)
    path = f"logs/live_{day}.log"
    if not os.path.exists(path):
        return out
    arm = re.compile(r"PRE-ARMED\s+\d\d-\d\d (\d\d:\d\d)\s+(\w+) \S+ buy-stop \$([\d.]+)\s+stop \$([\d.]+)")
    upd = re.compile(r"🔧 \d\d-\d\d (\d\d:\d\d)\s+(\w+) \S+ handle deepened -> stop \$([\d.]+)")
    for line in open(path, errors="ignore"):
        m = arm.search(line)
        if m:
            t = dtime(int(m[1][:2]), int(m[1][3:]))
            out[m[2]].append((t, float(m[3]), float(m[4])))
            continue
        m = upd.search(line)
        if m and out[m[2]]:
            t = dtime(int(m[1][:2]), int(m[1][3:]))
            out[m[2]].append((t, out[m[2]][-1][1], float(m[3])))   # trigger unchanged, stop moved
    return out


def leg_kinds(day: str) -> dict[str, set[str]]:
    """orderRef -> {'TP','SL',...} legs that filled, from data/paper_fills.csv."""
    out: dict[str, set] = defaultdict(set)
    if os.path.exists("data/paper_fills.csv"):
        for r in csv.DictReader(open("data/paper_fills.csv")):
            if r["time"].startswith(day):
                out[r["ref"]].add(r["leg"])
    return out


def vwap(fills):
    q = sum(f[0] for f in fills)
    return (sum(f[0] * f[1] for f in fills) / q if q else 0.0), q


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python real_ledger.py YYYY-MM-DD")
    day = sys.argv[1]
    ex = load_executions(day)
    if not ex:
        print(f"{day}: no executions — nothing armed filled that day")
        append_ledger(day, "armed", [])
        return
    levels = arm_levels(day)
    legs = leg_kinds(day)

    # ---- bracket trades: group by orderRef ----
    by_ref: dict[str, list[dict]] = defaultdict(list)
    loose: list[dict] = []                              # no ref = EOD market closes / churn
    for r in ex:
        (by_ref[r["ref"]] if r["ref"] else loose).append(r)
    loose.sort(key=lambda r: r["time"])
    used_loose = [False] * len(loose)

    def take_loose(sym: str, after: str, qty: int, side: str):
        """FIFO: the first `qty` shares of no-ref fills of `side` for `sym` after `after`."""
        got, need = [], qty
        for i, r in enumerate(loose):
            if used_loose[i] or r["symbol"] != sym or r["side"] != side or r["time"] < after:
                continue
            sh = min(int(r["shares"]), need)
            got.append((sh, float(r["price"]), r["time"]))
            if sh == int(r["shares"]):
                used_loose[i] = True
            else:
                loose[i]["shares"] = str(int(r["shares"]) - sh)
            need -= sh
            if need <= 0:
                break
        return got

    trades = []
    for ref, fills in sorted(by_ref.items(), key=lambda kv: min(f["time"] for f in kv[1])):
        m = re.match(r"cuph-(\w+)-(\w+)-\d{4}-\d\d-\d\d-rim(\d+)", ref)
        if not m:
            continue
        sym, tf = m[1], m[2]
        buys = [(int(f["shares"]), float(f["price"]), f["time"]) for f in fills if f["side"] == "BOT"]
        sells = [(int(f["shares"]), float(f["price"]), f["time"]) for f in fills if f["side"] == "SLD"]
        if not buys:
            continue
        entry, qty = vwap(buys)
        t_in = min(b[2] for b in buys)
        sold = sum(s_[0] for s_ in sells)
        leg = "TP" if "TP" in legs.get(ref, set()) else "STOP" if "SL" in legs.get(ref, set()) else ""
        exits = list(sells)
        kind = leg or ("TP" if sells and vwap(sells)[0] > entry else "STOP" if sells else "")
        if sold < qty:                                  # the rest closed by the EOD market flatten
            got = take_loose(sym, t_in, qty - sold, "SLD")
            if not got and not exits:
                trades.append(dict(symbol=sym, note=f"OPEN — no exit found for {qty} sh", entry=entry))
                continue
            exits += got
            kind = f"{leg}+EOD" if (leg and got) else ("EOD" if got else kind)
        exit_px, _ = vwap(exits)
        t_out = max(e_[2] for e_ in exits)
        # bracket levels: the latest arm/update for this symbol at or before the entry fill
        lv = [x for x in levels.get(sym, []) if x[0] <= dtime(int(t_in[:2]), int(t_in[3:5]))]
        if lv:
            trigger, stop = lv[-1][1], lv[-1][2]
        else:
            trigger, stop = entry, None
        risk = (trigger - stop) if stop is not None else None
        # peak R from the cached bars between entry and exit
        peak = ""
        if risk and risk > 0:
            try:
                from ib_async import IB, Stock
                b, _ = bars_15s(None, None, sym, datetime.fromisoformat(day).date()) \
                    if os.path.exists(f"cache/ibkr15s/{sym}/{day}.json") else (None, "not cached")
            except Exception:
                b = None
            if b is not None:
                t0, t1 = dtime(int(t_in[:2]), int(t_in[3:5])), dtime(int(t_out[:2]), int(t_out[3:5]))
                his = [b.h[i] for i in range(len(b)) if t0 <= b.ts[i].time() <= t1]
                if his:
                    peak = f"{(max(max(his), entry) - entry) / risk:.2f}"
        trades.append({
            "symbol": sym, "tf": tf, "entry_time": t_in[:5], "entry": f"{entry:.2f}",
            "trigger": f"{trigger:.2f}", "stop": f"{stop:.2f}" if stop is not None else "",
            "stop_pct": f"{risk / trigger * 100:.2f}" if risk else "",
            "exit_time": t_out[:5], "exit_kind": kind, "exit": f"{exit_px:.2f}",
            "R": f"{(exit_px - entry) / risk:.2f}" if risk else "", "peak_R": peak,
            "_qty": qty, "_pnl": (exit_px - entry) * qty,
        })

    rows = [t for t in trades if "note" not in t]
    append_ledger(day, "armed", [{k: v for k, v in t.items() if not k.startswith("_")} for t in rows])

    # ---- report ----
    print(f"{day} ARMED trades (real fills): {len(rows)}")
    print(f"  {'in':>5} {'sym':<5} {'qty':>5} {'entry':>8} {'exit':>8} {'kind':<5} {'R':>6} {'peak':>5}  {'P&L $':>8}")
    tot_r, tot_pnl = 0.0, 0.0
    for t in rows:
        r = float(t["R"]) if t["R"] else 0.0
        tot_r += r; tot_pnl += t["_pnl"]
        print(f"  {t['entry_time']:>5} {t['symbol']:<5} {t['_qty']:>5} {t['entry']:>8} {t['exit']:>8} "
              f"{t['exit_kind']:<5} {r:>+6.2f} {t['peak_R']:>5}  {t['_pnl']:>+8.0f}")
    print(f"  total {tot_r:+.2f}R   P&L {tot_pnl:+,.0f} $   (R = (exit-entry)/(trigger-stop) with real fills)")
    for t in trades:
        if "note" in t:
            print(f"  ⚠️ {t['symbol']}: {t['note']}")
    churn = [(r["symbol"], r["side"], int(r["shares"]), float(r["price"]))
             for i, r in enumerate(loose) if not used_loose[i] and int(r["shares"]) > 0]
    if churn:
        pnl = defaultdict(float); qty = defaultdict(int)
        for sym, side, sh, px in churn:
            sign = 1 if side == "SLD" else -1
            pnl[sym] += sign * sh * px; qty[sym] += -sign * sh
        print(f"  churn (no-ref fills beyond the matched exits — flatten storms, prior-day leftovers):")
        for sym in sorted(pnl):
            print(f"    {sym}: net {qty[sym]:+d} sh, cash {pnl[sym]:+,.0f} $")
    print(f"  📒 -> data/replay_trades.csv (variant armed)")


if __name__ == "__main__":
    main()
