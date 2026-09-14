"""
flatten_leftovers.py — START EVERY SESSION FLAT (user request 2026-09-14)
=========================================================================
Both robots flatten their own book at 15:49, but a close that IBKR never confirmed
survives the night — DUOL 247 sh on 2026-09-01, MU -250 sh on 2026-09-02, TER 81 sh
open since 2026-09-09. A leftover corrupts the next day's evaluation twice over: its
P&L lands in a session that did not choose the trade, and its shares occupy the
account while today's sizing is computed.

This closes what yesterday left behind, so the day's record is only the day's trading.

    python flatten_leftovers.py                 # DRY RUN — prints what it would close
    python flatten_leftovers.py --execute       # actually place the closing orders
    python flatten_leftovers.py --execute --exclude TER

SAFETY, all learned the hard way:
  * DRY RUN by default. Nothing is placed until you type --execute.
  * A symbol with ANY execution today is NOT a leftover and is skipped (--force to
    override). Run this at 11:00 by accident and it will refuse to touch live trades.
  * Closes route via SMART. ib.positions() hands back the LISTING exchange
    ('NASDAQ'), which is not a route — placing on it is rejected SILENTLY and the
    position survives with no stop behind it (the 2026-09-01 DUOL overnight).
  * A flatten is not a flatten until IBKR confirms it: every close is read back and
    anything still open is shouted.
  * Never reqGlobalCancel — the other robot's resting orders are not ours to kill.
  * Paper ports only. 4001/7496 are refused.
  * orderRef 'cleanup-<date>' so these fills are attributable forever and can never
    be mistaken for a cuph- strategy trade by real_ledger.py.

Run it pre-open: a market order placed before 09:30 queues and fills at the opening
print, which is the cleanest possible boundary between yesterday and today.
Every run appends to data/cleanup_log.csv — the leftovers' cash has to land somewhere.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
LIVE_PORTS = {4001, 7496}
CLEANUP_LOG = "data/cleanup_log.csv"


def leftovers(positions: dict, traded_today: set, exclude=()) -> list[tuple]:
    """Decide what to close. PURE — no IBKR, so the rules are testable.

    positions:    {symbol: qty}, qty < 0 = short
    traded_today: symbols with >= 1 execution today (those are LIVE, not leftovers)
    returns:      [(symbol, qty, action, skip_reason)] — skip_reason "" means close it
    """
    out = []
    for sym in sorted(positions):
        qty = int(positions[sym])
        if qty == 0:
            continue
        action = "SELL" if qty > 0 else "BUY"        # a short is closed by BUYING
        if sym in exclude:
            why = "excluded by --exclude"
        elif sym in traded_today:
            why = "traded TODAY — not a leftover (--force to close anyway)"
        else:
            why = ""
        out.append((sym, qty, action, why))
    return out


def record(rows: list[dict]) -> None:
    """Append this run to data/cleanup_log.csv — never overwrite; this is money."""
    cols = ["time", "symbol", "qty", "action", "status", "avg_fill", "ref", "note"]
    os.makedirs("data", exist_ok=True)
    new = not os.path.exists(CLEANUP_LOG)
    with open(CLEANUP_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", restval="")
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"  📒 recorded -> {CLEANUP_LOG}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Close positions left over from a previous session.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002, help="IB Gateway paper 4002 | TWS paper 7497")
    ap.add_argument("--client-id", type=int, default=70, help="must not clash with a running bot (8 / 18)")
    ap.add_argument("--execute", action="store_true",
                    help="actually place the closing orders (default: dry run, prints only)")
    ap.add_argument("--exclude", default="", help="comma-separated symbols to leave alone")
    ap.add_argument("--force", action="store_true",
                    help="close even symbols that already traded today (DANGEROUS mid-session)")
    ap.add_argument("--cancel-orders", action="store_true",
                    help="also cancel any resting order on a leftover symbol")
    ap.add_argument("--i-understand-live", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.port in LIVE_PORTS and not a.i_understand_live:
        sys.exit(f"✗ port {a.port} is a LIVE trading port — this tool is PAPER-only. Refusing.")
    exclude = {s.strip().upper() for s in a.exclude.split(",") if s.strip()}

    try:
        from ib_async import IB, Stock, MarketOrder, ExecutionFilter
    except ImportError:
        sys.exit("✗ ib_async not installed.  ->  pip install ib_async")

    ib = IB()
    try:
        ib.connect(a.host, a.port, clientId=a.client_id, timeout=10)
    except Exception as e:
        sys.exit(f"✗ can't reach IB Gateway/TWS at {a.host}:{a.port} — running? API enabled?  ({e})")

    now = datetime.now(ET)
    today = now.date().isoformat()
    print(f"LEFTOVER FLATTEN — {'🔴 EXECUTING' if a.execute else '🟢 DRY RUN (nothing will be placed)'}")
    print(f"  account {ib.managedAccounts()}  ·  {now:%Y-%m-%d %H:%M:%S} ET")

    pos = {p.contract.symbol: int(p.position) for p in ib.positions() if p.position != 0}
    cost = {p.contract.symbol: float(p.avgCost) for p in ib.positions() if p.position != 0}
    if not pos:
        print("\n  ✅ account is already FLAT — nothing left over, today starts clean.")
        ib.disconnect()
        return

    # Anything with a fill today is LIVE, not a leftover. This is what makes the tool
    # safe to run at any hour: it can never close a position the bots opened this session.
    traded_today = set()
    try:
        fills = ib.reqExecutions(ExecutionFilter())
        ib.sleep(2)
        traded_today = {f.contract.symbol for f in fills
                        if f.execution.time.astimezone(ET).date().isoformat() == today}
    except Exception as e:
        print(f"  ⚠️ could not read today's executions ({e}) — treating every position as a leftover")
    if a.force:
        traded_today = set()

    plan = leftovers(pos, traded_today, exclude)
    print(f"\n  {'sym':<7}{'qty':>7}{'avg cost':>11}  action")
    for sym, qty, action, why in plan:
        mark = "  →" if not why else "  ·"
        print(f"{mark}{sym:<7}{qty:>7}{cost.get(sym, 0):>11.2f}  "
              + (f"{action} {abs(qty)} to close" if not why else f"SKIP — {why}"))

    doomed = [(s, q, act) for s, q, act, why in plan if not why]
    if not doomed:
        print("\n  nothing to close — every open position is either today's or excluded.")
        ib.disconnect()
        return

    if a.cancel_orders:
        syms = {s for s, _, _ in doomed}
        for t in ib.openTrades():
            if t.contract.symbol in syms and t.orderStatus.status not in ("Filled", "Cancelled", "Inactive"):
                if a.execute:
                    try:
                        ib.cancelOrder(t.order)
                        print(f"  🗑️ cancelled resting {t.order.action} {t.contract.symbol} "
                              f"({t.order.orderRef or 'no ref'})")
                    except Exception as e:
                        print(f"  ⚠️ cancel failed for {t.contract.symbol}: {e}")
                else:
                    print(f"  would cancel resting {t.order.action} {t.contract.symbol} "
                          f"({t.order.orderRef or 'no ref'})")

    if not a.execute:
        print(f"\n  🟢 DRY RUN — {len(doomed)} position(s) would be closed. "
              f"Re-run with --execute to place the orders.")
        ib.disconnect()
        return

    if now.time() < dtime(9, 30):
        print("\n  ⏰ before the open — market orders will QUEUE and fill at the 09:30 print.")

    ref = f"cleanup-{today}"
    placed, rows = [], []
    for sym, qty, action in doomed:
        c = Stock(sym, "SMART", "USD")               # SMART, never the listing exchange
        try:
            ib.qualifyContracts(c)
            if not getattr(c, "conId", 0):
                raise ValueError("unknown contract")
        except Exception as e:
            print(f"  🚨 {sym}: cannot qualify ({e}) — CLOSE THIS ONE MANUALLY")
            rows.append({"time": f"{now:%Y-%m-%d %H:%M:%S}", "symbol": sym, "qty": qty,
                         "action": action, "status": "NOT PLACED", "ref": ref,
                         "note": f"qualify failed: {e}"})
            continue
        o = MarketOrder(action, abs(qty))
        o.orderRef = ref
        o.tif = "DAY"
        tr = ib.placeOrder(c, o)
        placed.append((sym, qty, tr))
        print(f"  ⛔ {action} {abs(qty)} {sym} placed (ref {ref})")

    ib.sleep(5)                                      # standalone script: a blocking sleep is fine here
    print()
    for sym, qty, tr in placed:
        st = tr.orderStatus.status
        avg = float(tr.orderStatus.avgFillPrice or 0)
        why = f" — {tr.log[-1].message}" if st != "Filled" and tr.log else ""
        print(f"  {'✅' if st == 'Filled' else '⏳' if st in ('PreSubmitted', 'Submitted') else '🚨'} "
              f"{sym} {qty:+d}: {st}{f' @ ${avg:.2f}' if avg else ''}{why}")
        rows.append({"time": f"{datetime.now(ET):%Y-%m-%d %H:%M:%S}", "symbol": sym, "qty": qty,
                     "action": "SELL" if qty > 0 else "BUY", "status": st,
                     "avg_fill": f"{avg:.4f}" if avg else "", "ref": ref,
                     "note": "queued for the open" if st in ("PreSubmitted", "Submitted") else ""})
    record(rows)

    try:
        left = {p.contract.symbol: int(p.position) for p in ib.positions() if p.position != 0}
    except Exception as e:
        print(f"  ⚠️ could not re-read positions to confirm: {e}")
        ib.disconnect()
        return
    still = {s: q for s, q in left.items() if s in {d[0] for d in doomed}}
    if still:
        print("\n  🚨 STILL OPEN — either the order is queued for the open, or it was rejected. "
              "Re-run after 09:31 and CHECK MANUALLY if it persists: "
              + ", ".join(f"{s} {q:+d}" for s, q in still.items()))
    else:
        print("\n  ✅ every leftover is closed — the account is flat for today's session.")
    ib.disconnect()


if __name__ == "__main__":
    main()
