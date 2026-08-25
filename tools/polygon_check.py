"""
polygon_check.py — READ-ONLY: does your Polygon plan stream LIVE data?
=====================================================================
The backfill only ever used Polygon's REST *historical* endpoint. Live trading needs the
*real-time websocket*, which is a SEPARATE entitlement — many plans are historical / 15-min
delayed only. This connects to the real-time stocks socket, authenticates, subscribes to a few
liquid names, and watches for live ticks. Places nothing, costs nothing beyond your plan.

Verdict logic:
  auth_failed                         -> plan does NOT include the real-time websocket
  auth ok + live ticks flow           -> ✅ real-time confirmed
  auth ok + subscribed + market shut  -> good sign; re-run in market hours to SEE ticks
  auth ok + market open + no ticks    -> entitled? check plan; unexpected

    python polygon_check.py [seconds=20]
"""
from __future__ import annotations
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)
import os, sys, json, time
import websocket                      # websocket-client (already installed)
try:
    import requests
except ImportError:
    requests = None

KEY = os.environ.get("POLYGON_API_KEY")
RT_URL = "wss://socket.polygon.io/stocks"        # real-time cluster (delayed plans use delayed.polygon.io)
SYMS = ["AAPL", "SPY", "QQQ", "NVDA", "TSLA"]
SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 20


def market_status() -> str:
    if not (requests and KEY):
        return "?"
    try:
        r = requests.get("https://api.polygon.io/v1/marketstatus/now", params={"apiKey": KEY}, timeout=10)
        return r.json().get("market", "?")
    except Exception as e:
        return f"? ({e})"


def main():
    if not KEY:
        sys.exit("✗ POLYGON_API_KEY not set in this shell (it lives in ~/.zshrc — run from an interactive shell).")

    mkt = market_status()
    print(f"market status now: {mkt}\n")

    print(f"connecting real-time  {RT_URL}")
    try:
        ws = websocket.create_connection(RT_URL, timeout=10)
    except Exception as e:
        sys.exit(f"✗ could not open the websocket: {e}")

    ws.settimeout(10)
    try:
        print("  <-", ws.recv().strip())                       # {"status":"connected"}
        ws.send(json.dumps({"action": "auth", "params": KEY}))
        auth = ws.recv().strip()
        print("  <-", auth)
    except Exception as e:
        ws.close(); sys.exit(f"✗ handshake failed: {e}")

    if "auth_success" not in auth:
        print("\n✗ real-time auth FAILED — this plan does NOT include the real-time websocket.")
        print("  Your REST/historical key is likely delayed-only. Options: upgrade the Polygon plan,")
        print("  test the 15-min delayed feed (wss://delayed.polygon.io), or use Alpaca's IEX stream.")
        ws.close(); return

    subs = ",".join(f"T.{s}" for s in SYMS) + "," + ",".join(f"AM.{s}" for s in SYMS)
    ws.send(json.dumps({"action": "subscribe", "params": subs}))
    print(f"  -> subscribed: trades + minute-bars for {', '.join(SYMS)}")

    live = mkt in ("open", "extended-hours")
    listen = SECONDS if live else 4                            # closed market: just catch the subscribe acks
    print(f"\nlistening {listen}s for live data ...")
    counts: dict[str, int] = {}
    statuses: list[str] = []
    deadline = time.time() + listen
    ws.settimeout(2)
    while time.time() < deadline:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except Exception:
            break
        for m in json.loads(raw):
            ev = m.get("ev")
            if ev == "status":
                statuses.append(str(m.get("message", m)))
            else:
                counts[ev] = counts.get(ev, 0) + 1
    ws.close()

    for s in statuses:
        print("  status:", s)
    trades, bars = counts.get("T", 0), counts.get("AM", 0)
    other = sum(v for k, v in counts.items() if k not in ("T", "AM"))
    print(f"\nlive messages: {trades} trades, {bars} minute-bars, {other} other")

    denied = any("not authorized" in s.lower() or "access real-time" in s.lower() for s in statuses)
    if trades or bars:
        print("✅ REAL-TIME CONFIRMED — your Polygon plan streams live data. Use it for live detection.")
    elif denied:
        print("✗ real-time NOT included — Polygon REJECTED the subscription (this plan is DELAYED-only).")
        print("  (auth succeeds, but the real-time channels are not entitled.) Options:")
        print("   • use Alpaca's free IEX real-time stream for paper trading (no extra cost), OR")
        print("   • upgrade the Polygon plan to one with real-time, OR test wss://delayed.polygon.io.")
    elif not live:
        print(f"… auth + subscribe accepted, but the market is '{mkt}', so no live ticks right now.")
        print("   Re-run 9:30–16:00 ET to watch ticks flow.")
    else:
        print("⚠️ auth ok but NO ticks during open market — unexpected. Check plan entitlements.")


if __name__ == "__main__":
    main()
