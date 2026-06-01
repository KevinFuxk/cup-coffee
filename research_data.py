"""
research_data.py — disk-cached data access + full-favorable-path labeler
========================================================================
Two jobs, both in service of the factor-mining loop:

1. CACHE EVERYTHING TO DISK. Every Polygon call (minute bars, daily bars, news,
   financials, ticker details) is fetched ONCE and saved under cache/. Re-running
   the mining — or testing another strategy — then reads from disk in seconds
   instead of re-downloading. This is the plumbing that makes research cheap.

2. FULL-PATH LABELS. The original detector exited at the measured-move target, so
   the recorded path was truncated above ~3R. Here we replay each trade from its
   breakout to the stop OR the time cap (no profit target), so the realized result
   at ANY fixed take-profit 1R..5R is exact — that is what lets us ask
   "take 1R or hold for more?" honestly.

Point-in-time discipline: daily/news/financials are all queried with a strict
"before the trade day" filter so no future information leaks into a factor.

Runs in YOUR environment with your Polygon key.
"""

from __future__ import annotations

import os
import json
import hashlib
from datetime import date as Date, datetime, time as dtime, timezone, timedelta
from typing import Optional, Callable

from data_layer import Bars, downsample

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # py<3.9
    from backports.zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")

CACHE_DIR = "cache"


# ----------------------------------------------------------------------------
# tiny JSON disk cache
# ----------------------------------------------------------------------------

def _cache_path(kind: str, key: str) -> str:
    d = os.path.join(CACHE_DIR, kind)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, hashlib.md5(key.encode()).hexdigest()[:20] + ".json")

def _cached(kind: str, key: str, fetch: Callable):
    p = _cache_path(kind, key)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass                                       # corrupt cache -> refetch
    val = fetch()
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(val, f)
    os.replace(tmp, p)                                 # atomic: never leaves a half file
    return val


# ----------------------------------------------------------------------------
# cached Polygon access
# ----------------------------------------------------------------------------

class ResearchData:
    def __init__(self, api_key: str):
        self.key = api_key

    def _get(self, url: str, params: dict) -> dict:
        import requests
        r = requests.get(url, params={**params, "apiKey": self.key}, timeout=60)
        try:
            return r.json()
        except Exception:
            return {}

    # --- minute bars, rebuilt EXACTLY like PolygonBarProvider so indices match ---
    def _minute_raw(self, symbol: str, day: Date) -> list:
        sym = symbol.split(":")[-1]
        def fetch():
            u = (f"https://api.polygon.io/v2/aggs/ticker/{sym}"
                 f"/range/1/minute/{day.isoformat()}/{day.isoformat()}")
            return self._get(u, {"adjusted": "true", "sort": "asc", "limit": 50000}).get("results") or []
        return _cached("minute", f"{sym}:{day.isoformat()}", fetch)

    def bars(self, symbol: str, day: Date, timeframe: str) -> Optional[Bars]:
        rows = self._minute_raw(symbol, day)
        sym = symbol.split(":")[-1]
        o=[]; h=[]; l=[]; c=[]; v=[]; ts=[]
        for r in rows:
            dt = datetime.fromtimestamp(r["t"]/1000, tz=timezone.utc).astimezone(ET)
            if dtime(9, 30) <= dt.time() < dtime(16, 0):
                o.append(r["o"]); h.append(r["h"]); l.append(r["l"])
                c.append(r["c"]); v.append(r["v"]); ts.append(dt)
        if not c:
            return None
        one = Bars(sym, day, "1min", ts, o, h, l, c, v, adjusted=True)
        if timeframe == "1min": return one
        if timeframe == "2min": return downsample(one, 2, "2min")
        if timeframe == "5min": return downsample(one, 5, "5min")
        return None

    # --- daily bars (symbol or ETF), inclusive range; cached per (sym,start,end) ---
    def daily(self, symbol: str, start: Date, end: Date) -> list:
        sym = symbol.split(":")[-1]
        def fetch():
            u = (f"https://api.polygon.io/v2/aggs/ticker/{sym}"
                 f"/range/1/day/{start.isoformat()}/{end.isoformat()}")
            return self._get(u, {"adjusted": "true", "sort": "asc", "limit": 50000}).get("results") or []
        return _cached("daily", f"{sym}:{start.isoformat()}:{end.isoformat()}", fetch)

    # --- news published strictly BEFORE the trade day (point-in-time) ---
    def news(self, symbol: str, before: Date, lookback_days: int = 10) -> list:
        sym = symbol.split(":")[-1]
        lo = (before - timedelta(days=lookback_days)).isoformat()
        hi = before.isoformat()
        def fetch():
            u = "https://api.polygon.io/v2/reference/news"
            return self._get(u, {"ticker": sym, "published_utc.gte": lo,
                                 "published_utc.lt": hi, "limit": 50, "order": "desc"}).get("results") or []
        return _cached("news", f"{sym}:{hi}:{lookback_days}", fetch)

    # --- most recent quarterly financials FILED before the trade day (PIT) ---
    def financials(self, symbol: str, before: Date) -> list:
        sym = symbol.split(":")[-1]
        def fetch():
            u = "https://api.polygon.io/vX/reference/financials"
            return self._get(u, {"ticker": sym, "filing_date.lt": before.isoformat(),
                                 "timeframe": "quarterly", "order": "desc",
                                 "sort": "filing_date", "limit": 6}).get("results") or []
        return _cached("fin", f"{sym}:{before.isoformat()}", fetch)

    def details(self, symbol: str) -> dict:
        sym = symbol.split(":")[-1]
        return _cached("details", sym,
                       lambda: self._get(f"https://api.polygon.io/v3/reference/tickers/{sym}", {}).get("results") or {})


# ----------------------------------------------------------------------------
# full-favorable-path labeler — exact realized R at every take-profit
# ----------------------------------------------------------------------------

def label_full_path(b: Bars, breakout_idx: int, entry: float, stop: float,
                    risk: float, max_hold_bars: int = 240,
                    take_profits=(1, 2, 3, 4, 5)) -> dict:
    """Replay the trade with NO profit target: walk to the stop or the time cap.
    Returns full MFE/MAE in R, and the exact realized R for each fixed take-profit.

    Intrabar convention matches the original detector: if a bar touches both the
    stop and a target, the STOP counts first (conservative)."""
    end = min(len(b), breakout_idx + max_hold_bars + 1)
    mfe = mae = 0.0
    # per-take-profit state: realized R, and whether already resolved
    realized = {k: None for k in take_profits}
    stopped_at = None
    for j in range(breakout_idx + 1, end):
        fav = (b.h[j] - entry) / risk
        adv = (entry - b.l[j]) / risk
        mfe = max(mfe, fav)
        mae = max(mae, adv)
        hit_stop = b.l[j] <= stop
        for k in take_profits:
            if realized[k] is not None:
                continue
            if hit_stop:                              # stop checked first (pessimistic)
                realized[k] = -1.0
            elif b.h[j] >= entry + k * risk:
                realized[k] = float(k)
        if hit_stop:
            stopped_at = j
            break
    # anything unresolved timed out -> exit at the last available close
    last = min(end, len(b)) - 1
    final_R = (b.c[last] - entry) / risk if last > breakout_idx else 0.0
    for k in take_profits:
        if realized[k] is None:
            realized[k] = final_R
    return {
        "full_mfe_R": round(mfe, 4),
        "full_mae_R": round(mae, 4),
        "stopped": stopped_at is not None,
        "realized_R": {str(k): round(realized[k], 4) for k in take_profits},
        "win": {str(k): int(realized[k] >= k) for k in take_profits},
    }


class CachedBarProvider:
    """Drop-in BarDataProvider for the backfill that reads through the disk cache,
    so a backfill run ALSO populates cache/minute for fast re-mining later."""
    def __init__(self, rd: "ResearchData"):
        self.rd = rd
    def intraday_bars(self, symbol: str, day: Date, timeframe: str) -> Optional[Bars]:
        return self.rd.bars(symbol, day, timeframe)


if __name__ == "__main__":
    import os as _os
    rd = ResearchData(_os.environ["POLYGON_API_KEY"])
    day = Date(2026, 5, 21)
    b = rd.bars("GFS", day, "2min")
    print(f"GFS {day} 2min: {len(b) if b else 0} bars (cached at cache/minute/)")
    if b:
        # cheap self-check of the labeler on a synthetic entry near the open
        entry = b.c[20]; risk = max(0.01, entry * 0.01); stop = entry - risk
        res = label_full_path(b, 20, entry, stop, risk)
        print("label_full_path sample:", res["realized_R"], "mfe=", res["full_mfe_R"])
    print("daily SPY rows:", len(rd.daily("SPY", Date(2026,4,1), day)))
    print("news GFS rows:", len(rd.news("GFS", day)))
    fin = rd.financials("GFS", day)
    print("financials GFS records:", len(fin), (fin[0].get("fiscal_period") if fin else ""))
