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


# ---- intraday SESSION RULE (the strategy's trading window) ----
# ONE all-day session: a cup/handle entry can trigger ANY time during RTH (9:30-15:49) —

# NO midday (11:00-13:00) no-trade window. Every position is held until flat by 15:49 ET

# (or the max-hold bar cap); a morning entry now rides THROUGH lunch instead of closing at 11:00.
_EOD_EXIT = dtime(15, 49)

def session_end_idx(b, entry_idx, max_hold_bars):
    """Last bar this trade may hold to: flat by 15:49 ET (or the max-hold cap). No entry-time
    restriction — entries allowed all day, morning trades held through lunch.

    NOTE: this controls the EXIT bar only, NOT the entry. There is no 11:00-13:00 no-trade
    window in this code — that 'lunch rule' is toggled at the data-file level (see app.py).
    """
    last = entry_idx
    # Walk forward from entry; keep advancing `last` while we're still at/before 15:49 and
    # STOP the instant a bar crosses 15:49 -> `last` can never end up past the cutoff.
    for i in range(entry_idx, len(b)):
        if b.ts[i].time() <= _EOD_EXIT:
            last = i
        else:
            break
    # Three independent "you must stop BY here" ceilings; obey all -> take the EARLIEST (min):
    #   last                      -> the 15:49 time-of-day rule (flat by close)
    #   entry_idx + max_hold_bars -> the 240-bar max-hold time-stop
    #   len(b) - 1                -> never run off the end of the bar array (safety)
    return min(last, entry_idx + max_hold_bars, len(b) - 1)


# ----------------------------------------------------------------------------
# tiny JSON disk cache
# ----------------------------------------------------------------------------
# "Download once, remember forever." A generic remember-it layer that knows nothing
# about markets: callers hand it a unique key + a `fetch` recipe, and it returns the
# saved answer from disk if it exists, else runs the recipe once and saves the result.

def _cache_path(kind: str, key: str) -> str:
    # Decide WHERE one answer lives on disk: cache/<kind>/<hash-of-key>.json
    d = os.path.join(CACHE_DIR, kind)                  # `kind` groups items -> a subfolder
    os.makedirs(d, exist_ok=True)                      # create the folder if missing
    # Hash the key so any key (with ':' , '/', long text) becomes a safe, fixed-length,
    # repeatable filename -> the same key always maps to the same file.
    return os.path.join(d, hashlib.md5(key.encode()).hexdigest()[:20] + ".json")

def _cached(kind: str, key: str, fetch: Callable):
    # `fetch` is a FUNCTION the caller passes in (a "how to download this" recipe).
    # _cached itself knows no URLs; it only calls `fetch()` on a cache MISS.
    p = _cache_path(kind, key)
    if os.path.exists(p):                              # cache HIT?
        try:
            with open(p) as f:
                return json.load(f)                    # yes -> return from disk, NO network
        except Exception:
            pass                                       # corrupt cache -> fall through, refetch
    val = fetch()                                      # cache MISS -> actually download now
    tmp = p + ".tmp"                                   # write to temp first, then rename, so a
    with open(tmp, "w") as f:                          # crash mid-write never leaves a half file
        json.dump(val, f)
    os.replace(tmp, p)                                 # atomic: never leaves a half file
    return val


# ----------------------------------------------------------------------------
# cached Polygon access
# ----------------------------------------------------------------------------
# One "front desk" for all market data. Every public method below follows the same
# shape: clean the symbol -> define a `fetch` recipe -> return _cached(kind, key, fetch).
# So every download is cached automatically, and point-in-time filters keep the future out.

class ResearchData:
    def __init__(self, api_key: str, fmp_key: Optional[str] = None):
        self.key = api_key                            # Polygon key (required)
        self.fmp = fmp_key                            # FMP key (optional; only earnings need it)

    # Polygon doorway: tries ONCE, auto-attaches the key, returns {} on any failure
    # (so callers can safely do ._get(...).get("results") without try/except everywhere).
    def _get(self, url: str, params: dict) -> dict:
        import requests
        r = requests.get(url, params={**params, "apiKey": self.key}, timeout=60)
        try:
            return r.json()
        except Exception:
            return {}                                 # bad/empty response -> looks like "no data"

    # --- FMP (working `stable` API; retried because the plan rate-limits) ---
    # FMP doorway: separate from _get because FMP has a different URL and rate-limits us,
    # so it RETRIES 3x with a 0.7s backoff and returns None if all attempts fail.
    def _fmp(self, path: str, **params):
        import requests, time
        params["apikey"] = self.fmp                   # note: FMP spells it 'apikey' (lowercase)
        for _ in range(3):                            # up to 3 attempts
            try:
                r = requests.get(f"https://financialmodelingprep.com/stable/{path}",
                                 params=params, timeout=30)
                if r.status_code == 200:              # only 200 = success -> return & exit loop
                    return r.json()
            except Exception:
                pass                                  # network error -> don't crash, just retry
            time.sleep(0.7)                           # backoff before the next attempt
        return None                                   # all 3 failed

    def earnings_symbols(self, day: Date) -> set:
        """Set of tickers that REPORTED earnings on `day` (FMP stable, cached)."""
        def fetch():
            data = self._fmp("earnings-calendar", **{"from": day.isoformat(), "to": day.isoformat()})
            return sorted({r["symbol"] for r in data if isinstance(r, dict) and r.get("symbol")}) \
                if isinstance(data, list) else []
        return set(_cached("fmp_earn", day.isoformat(), fetch))

    def revenue_growth_yoy(self, symbol: str, before: Date) -> Optional[float]:
        """YoY revenue growth from POLYGON financials (unlimited, point-in-time): latest
        quarter filed before `before` vs ~4 quarters earlier. None if unavailable."""
        fins = self.financials(symbol, before)        # Polygon vX, quarterly, filed before `before`, cached
        def rev(r):
            try:
                return r["financials"]["income_statement"]["revenues"]["value"]
            except (KeyError, TypeError):
                return None
        recs = [r for r in fins if rev(r)]
        if len(recs) < 5:
            return None
        r0, r4 = rev(recs[0]), rev(recs[4])
        return (r0 / r4 - 1.0) if (r0 and r4) else None

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
        lo = (before - timedelta(days=lookback_days)).isoformat()   # window start
        hi = before.isoformat()                                     # window end = the trade day
        def fetch():
            u = "https://api.polygon.io/v2/reference/news"
            # KEY no-lookahead guard: published_utc.lt = hi -> only news that existed STRICTLY
            # before the trade day is visible, so a factor can't secretly peek at the future.
            return self._get(u, {"ticker": sym, "published_utc.gte": lo,
                                 "published_utc.lt": hi, "limit": 50, "order": "desc"}).get("results") or []
        return _cached("news", f"{sym}:{hi}:{lookback_days}", fetch)

    # --- most recent quarterly financials FILED before the trade day (PIT) ---
    def financials(self, symbol: str, before: Date) -> list:
        sym = symbol.split(":")[-1]
        def fetch():
            u = "https://api.polygon.io/vX/reference/financials"
            # Same no-lookahead guard as news: filing_date.lt = before -> only filings made
            # before the trade day. Last 6 quarters is enough for the YoY revenue comparison.
            return self._get(u, {"ticker": sym, "filing_date.lt": before.isoformat(),
                                 "timeframe": "quarterly", "order": "desc",
                                 "sort": "filing_date", "limit": 6}).get("results") or []
        return _cached("fin", f"{sym}:{before.isoformat()}", fetch)

    def details(self, symbol: str) -> dict:
        sym = symbol.split(":")[-1]
        return _cached("details", sym,
                       lambda: self._get(f"https://api.polygon.io/v3/reference/tickers/{sym}", {}).get("results") or {})

    # --- corporate splits (forward + reverse), all-time, cached per ticker ---
    def splits(self, symbol: str) -> list:
        """Every stock split Polygon has for this ticker. A REVERSE split has
        split_to < split_from (e.g. 100->1). Used to (a) recover the REAL un-adjusted
        price and (b) count reverse splits as a distress flag."""
        sym = symbol.split(":")[-1]
        def fetch():
            u = "https://api.polygon.io/v3/reference/splits"
            return self._get(u, {"ticker": sym, "limit": 1000,
                                 "order": "desc", "sort": "execution_date"}).get("results") or []
        return _cached("splits", sym, fetch)


# ----------------------------------------------------------------------------
# full-favorable-path labeler — exact realized R at every take-profit
# ----------------------------------------------------------------------------

def label_full_path(b: Bars, breakout_idx: int, entry: float, stop: float,
                    risk: float, max_hold_bars: int = 240,
                    take_profits=(1, 2, 3, 4, 5)) -> dict:
    """Replay the trade with NO profit target: walk to the stop or the time cap.
    Returns full MFE/MAE in R, and the exact realized R for each fixed take-profit.

    Intrabar convention matches the original detector: if a bar touches both the
    stop and a target, the STOP counts first (conservative).
    Hold is capped at the 240-bar limit AND flat by 3:49pm ET (intraday only)."""
    # Big idea: ONE replay answers ALL take-profits. We never exit early at a profit target;
    # we only stop on the STOP or the time cap, recording what each 1R..5R target WOULD have done.
    se = session_end_idx(b, breakout_idx, max_hold_bars)
    end = (se + 1) if se is not None else (breakout_idx + 1)   # +1 = exclusive loop bound (incl. bar se)
    mfe = mae = 0.0                                   # running peak favorable / adverse excursion (in R)
    # per-take-profit state: realized R, None = "not decided yet"
    realized = {k: None for k in take_profits}
    stopped_at = None
    for j in range(breakout_idx + 1, end):           # walk every bar AFTER entry to the deadline
        fav = (b.h[j] - entry) / risk                # this bar's high, in R, in our favor
        adv = (entry - b.l[j]) / risk                # this bar's low, in R, against us
        mfe = max(mfe, fav)                          # best the trade EVER reached
        mae = max(mae, adv)                          # worst heat the trade EVER took
        hit_stop = b.l[j] <= stop                    # did this bar's low reach the stop?
        for k in take_profits:
            if realized[k] is not None:
                continue                             # this target already decided on an earlier bar
            if hit_stop:                              # stop checked FIRST (pessimistic): if a bar
                realized[k] = -1.0                    # spans both stop & target, assume the loss
            elif b.h[j] >= entry + k * risk:          # else did the high reach the k-R target?
                realized[k] = float(k)                # -> resolved at +kR
        if hit_stop:
            stopped_at = j
            break                                    # stopped out -> trade over, nothing more to track
    # anything unresolved timed out -> exit at the last available close
    last = min(end, len(b)) - 1                       # last valid bar index (guard vs end-of-data)
    final_R = (b.c[last] - entry) / risk if last > breakout_idx else 0.0   # close-based time-out exit
    for k in take_profits:
        if realized[k] is None:                       # never hit target, never stopped -> timed out
            realized[k] = final_R
    return {
        "full_mfe_R": round(mfe, 4),
        "full_mae_R": round(mae, 4),
        "stopped": stopped_at is not None,
        "realized_R": {str(k): round(realized[k], 4) for k in take_profits},
        "win": {str(k): int(realized[k] >= k) for k in take_profits},
    }


# Adapter: the backfill expects a provider with `intraday_bars(...)`, but our data is in
# ResearchData.bars(...). This bridges the two so the backfill reads THROUGH the cache
# (and thereby fills cache/minute as a side effect, warming it for fast re-mining later).
class CachedBarProvider:
    """Drop-in BarDataProvider for the backfill that reads through the disk cache,
    so a backfill run ALSO populates cache/minute for fast re-mining later."""
    def __init__(self, rd: "ResearchData"):
        self.rd = rd                                   # the cached data front desk
    def intraday_bars(self, symbol: str, day: Date, timeframe: str) -> Optional[Bars]:
        return self.rd.bars(symbol, day, timeframe)    # just delegate to the cached method


# Smoke test: runs ONLY when you execute this file directly (`python research_data.py`),
# never on import. Confirms the Polygon key works and every endpoint + the labeler function.
if __name__ == "__main__":
    import os as _os
    rd = ResearchData(_os.environ["POLYGON_API_KEY"])  # build the front desk from your key
    day = Date(2026, 5, 21)
    b = rd.bars("GFS", day, "2min")                    # exercise bars() (also fills the cache)
    print(f"GFS {day} 2min: {len(b) if b else 0} bars (cached at cache/minute/)")
    if b:
        # cheap self-check of the labeler on a synthetic entry near the open
        entry = b.c[20]; risk = max(0.01, entry * 0.01); stop = entry - risk   # fake entry @ bar 20
        res = label_full_path(b, 20, entry, stop, risk)
        print("label_full_path sample:", res["realized_R"], "mfe=", res["full_mfe_R"])
    print("daily SPY rows:", len(rd.daily("SPY", Date(2026,4,1), day)))   # exercise daily()
    print("news GFS rows:", len(rd.news("GFS", day)))                     # exercise news()
    fin = rd.financials("GFS", day)                                       # exercise financials()
    print("financials GFS records:", len(fin), (fin[0].get("fiscal_period") if fin else ""))
