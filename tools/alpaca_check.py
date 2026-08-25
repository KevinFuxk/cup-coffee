"""
alpaca_check.py — READ-ONLY Alpaca paper connection smoke-test (NO orders)
==========================================================================
The first broker rung: prove we can log in to the PAPER account and READ it. Places no orders,
cancels nothing, touches no position. Run this the moment your keys are set — if it prints your
paper balances and the market clock, the connection works and we move on to the order adapter.

Setup first (your actions — keys are secrets, like POLYGON_API_KEY):
  1) free paper account at alpaca.markets -> generate PAPER api keys
  2) add to ~/.zshrc:
        export APCA_API_KEY_ID="..."
        export APCA_API_SECRET_KEY="..."
  3) pip install alpaca-py
Then:  python alpaca_check.py

SAFETY: hard-pinned to paper=True (the paper-api.alpaca.markets endpoint). Going live is a
deliberate, separate change — never a flag you flip by accident.
"""
from __future__ import annotations
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)
import os, sys


def main():
    try:
        from alpaca.trading.client import TradingClient
    except ImportError:
        sys.exit("✗ alpaca-py not installed.  ->  pip install alpaca-py")

    kid = os.environ.get("APCA_API_KEY_ID")
    sec = os.environ.get("APCA_API_SECRET_KEY")
    if not kid or not sec:
        sys.exit("✗ keys not found. Add APCA_API_KEY_ID and APCA_API_SECRET_KEY to ~/.zshrc, "
                 "then open a new shell (or `source ~/.zshrc`).")

    client = TradingClient(kid, sec, paper=True)        # SAFETY: paper endpoint, hard-pinned
    try:
        acct = client.get_account()
    except Exception as e:
        sys.exit(f"✗ connection/auth failed: {e}\n   (check the keys are PAPER keys and copied correctly)")

    print("✅ connected to Alpaca PAPER\n")
    print(f"  status            {acct.status}")
    print(f"  cash              ${float(acct.cash):,.2f}")
    print(f"  equity            ${float(acct.equity):,.2f}")
    print(f"  buying power      ${float(acct.buying_power):,.2f}")
    print(f"  margin multiplier {acct.multiplier}x        <- your REAL max leverage on this account")
    print(f"  pattern day trader {acct.pattern_day_trader}")
    print(f"  trading blocked   {acct.trading_blocked}")

    clock = client.get_clock()
    print(f"\n  market open now   {clock.is_open}")
    print(f"  next open         {clock.next_open}")
    print(f"  next close        {clock.next_close}")

    positions = client.get_all_positions()
    orders = client.get_orders()
    print(f"\n  open positions    {len(positions)}")
    for p in positions:
        print(f"    {p.symbol:6} {p.qty} @ ${float(p.avg_entry_price):.2f}  (mkt ${float(p.market_value):,.0f})")
    print(f"  open orders       {len(orders)}")
    for o in orders:
        print(f"    {o.symbol:6} {o.side} {o.qty} {o.type} {o.status}")

    print("\nread-only check complete — no orders placed, nothing changed.")
    print("next rung: the order adapter (buy-stop entry -> OCO bracket exit, idempotent, with a kill-switch).")


if __name__ == "__main__":
    main()
