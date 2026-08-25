"""
providers_real.py — wire REAL market data into Cup Coffee
=========================================================
Fills the two provider stubs the pipeline left open:

  Stage 1 universe   -> TradingViewUniverseProvider   (live daily scan)
  Stage 2 bars       -> PolygonBarProvider            (1-min intraday)
  + helpers          -> fmp_had_earnings, polygon_energy_beta

These talk to real APIs, so they run in YOUR environment with YOUR keys — not
here. Set the keys, `pip install requests tradingview-screener`, and plug the
providers into each stage's builder.

Honesty notes that keep the backtest truthful:
  * Polygon includes delisted tickers -> good for survivorship-correct history.
  * TradingView gives only the CURRENT snapshot -> fine for LIVE, not history.
    For the backtest, build the universe from Polygon grouped daily bars +
    point-in-time fundamentals instead.
  * Revenue growth & change from TradingView are PERCENTS (38.8), so we /100
    to match the config's fractions (0.40).
"""

from __future__ import annotations
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)

from datetime import date as Date, datetime, time, timezone, timedelta
from typing import Optional, Callable

from universe import SecuritySnapshot
from data_layer import Bars, downsample


# ----------------------------------------------------------------------------
# Stage 2 — intraday bars from Polygon
# ----------------------------------------------------------------------------

class PolygonBarProvider:
    """1-minute bars for one symbol/day, RTH-filtered, then downsampled to 2/5."""

    def __init__(self, api_key: str):
        self.key = api_key

    def intraday_bars(self, symbol: str, day: Date, timeframe: str) -> Optional[Bars]:
        import requests
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo  # py<3.9
        et = ZoneInfo("America/New_York")

        sym = symbol.split(":")[-1]                       # NASDAQ:AAPL -> AAPL
        url = (f"https://api.polygon.io/v2/aggs/ticker/{sym}"
               f"/range/1/minute/{day.isoformat()}/{day.isoformat()}")
        resp = requests.get(url, params={"adjusted": "true", "sort": "asc",
                                         "limit": 50000, "apiKey": self.key}, timeout=30)
        rows = resp.json().get("results", []) or []

        o=[]; h=[]; l=[]; c=[]; v=[]; ts=[]
        for r in rows:
            dt = datetime.fromtimestamp(r["t"]/1000, tz=timezone.utc).astimezone(et)
            if time(9, 30) <= dt.time() < time(16, 0):    # RTH only
                o.append(r["o"]); h.append(r["h"]); l.append(r["l"])
                c.append(r["c"]); v.append(r["v"]); ts.append(dt)
        if not c:
            return None
        one = Bars(sym, day, "1min", ts, o, h, l, c, v, adjusted=True)
        if timeframe == "1min": return one
        if timeframe == "2min": return downsample(one, 2, "2min")
        if timeframe == "5min": return downsample(one, 5, "5min")
        return None


# ----------------------------------------------------------------------------
# Stage 1 — live universe scan from TradingView
# ----------------------------------------------------------------------------

class TradingViewUniverseProvider:
    """LIVE daily scan. Pass an earnings checker and an energy-beta function
    (helpers below) so had_earnings / energy_beta work."""

    def __init__(self,
                 had_earnings_fn: Optional[Callable[[str, Date], bool]] = None,
                 energy_beta_fn: Optional[Callable[[str, Date, int], Optional[float]]] = None):
        self._earn = had_earnings_fn or (lambda s, d: False)
        self._beta = energy_beta_fn or (lambda s, d, w: None)

    def universe_snapshot(self, as_of: Date) -> list[SecuritySnapshot]:
        from tradingview_screener import Query, col
        q = (Query()
             .select("name", "close", "market_cap_basic", "average_volume_30d_calc",
                     "sector", "total_revenue_yoy_growth_ttm", "change", "typespecs", "exchange")
             .where(col("close") >= 15,
                    col("typespecs").has(["common"]),
                    col("exchange").isin(["NASDAQ", "NYSE"]),
                    col("average_volume_30d_calc") >= 500_000)
             .limit(5000))
        _, df = q.get_scanner_data()

        snaps = []
        for _, row in df.iterrows():
            rev = row.get("total_revenue_yoy_growth_ttm")
            chg = row.get("change")
            snaps.append(SecuritySnapshot(
                symbol=row["ticker"],
                close=float(row["close"]),
                market_cap=float(row.get("market_cap_basic") or 0),
                avg_dollar_volume=float(row["close"]) * float(row.get("average_volume_30d_calc") or 0),
                sector=row.get("sector") or "",
                revenue_growth_yoy=(float(rev)/100 if rev is not None else None),  # % -> fraction
                gap_pct=(float(chg)/100 if chg is not None else None),             # % -> fraction
                is_common=True,
            ))
        return snaps

    def had_earnings(self, symbol: str, as_of: Date) -> bool:
        return self._earn(symbol, as_of)

    def energy_beta(self, symbol: str, as_of: Date, window_days: int) -> Optional[float]:
        return self._beta(symbol, as_of, window_days)


# ----------------------------------------------------------------------------
# Helpers — earnings calendar (FMP) and energy beta (Polygon)
# ----------------------------------------------------------------------------

def fmp_had_earnings(api_key: str):
    """Returns a function: did `symbol` report on `as_of`?  (FMP earnings calendar)"""
    def _check(symbol: str, as_of: Date) -> bool:
        import requests
        sym = symbol.split(":")[-1]
        url = f"https://financialmodelingprep.com/api/v3/historical/earning_calendar/{sym}"
        data = requests.get(url, params={"apikey": api_key}, timeout=30).json()
        # FMP's v3 calendar is a LEGACY endpoint — current keys get a 403 error *dict*,
        # not a list. Tolerate any non-list response so a dead/rate-limited endpoint
        # degrades to "no earnings" instead of throwing and silently killing the backfill.
        if not isinstance(data, list):
            return False
        return any(isinstance(r, dict) and r.get("date") == as_of.isoformat() for r in data)
    return _check


def polygon_energy_beta(api_key: str, benchmark: str = "USO"):
    """Returns a function: rolling beta of `symbol` returns vs an energy ETF."""
    def _beta(symbol: str, as_of: Date, window_days: int) -> Optional[float]:
        import requests
        def daily_closes(sym):
            start = (as_of - timedelta(days=window_days*2)).isoformat()
            url = (f"https://api.polygon.io/v2/aggs/ticker/{sym.split(':')[-1]}"
                   f"/range/1/day/{start}/{as_of.isoformat()}")
            rows = requests.get(url, params={"adjusted": "true", "sort": "asc",
                                             "apiKey": api_key}, timeout=30).json().get("results", [])
            return [r["c"] for r in rows][-(window_days+1):]
        s = daily_closes(symbol); b = daily_closes(benchmark)
        n = min(len(s), len(b))
        if n < 10:
            return None
        sr = [s[i]/s[i-1]-1 for i in range(1, n)]
        br = [b[i]/b[i-1]-1 for i in range(1, n)]
        mb = sum(br)/len(br); ms = sum(sr)/len(sr)
        cov = sum((sr[i]-ms)*(br[i]-mb) for i in range(len(br)))
        var = sum((x-mb)**2 for x in br)
        return cov/var if var else None
    return _beta


# ----------------------------------------------------------------------------
# Wiring example (do NOT run here — needs your keys + network)
# ----------------------------------------------------------------------------
WIRING_EXAMPLE = '''
from datetime import date
from universe import DailyUniverseBuilder
from data_layer import DataLayer
from providers_real import (TradingViewUniverseProvider, PolygonBarProvider,
                            fmp_had_earnings, polygon_energy_beta)
from cup_coffee_config_v2 import CONFIG

POLY = "YOUR_POLYGON_KEY"; FMP = "YOUR_FMP_KEY"

uni_provider = TradingViewUniverseProvider(
    had_earnings_fn = fmp_had_earnings(FMP),
    energy_beta_fn  = polygon_energy_beta(POLY),
)
watchlist = DailyUniverseBuilder(CONFIG, uni_provider).build(date.today())

bar_provider = PolygonBarProvider(POLY)
series = DataLayer(CONFIG, bar_provider).load_watchlist(
    [(c.symbol, watchlist.as_of) for c in watchlist.tradable()]
)
# series -> PatternDetector -> events -> FactorScorer -> FactorLoop
'''

if __name__ == "__main__":
    print("Real providers ready. Wire them like this (needs your keys):")
    print(WIRING_EXAMPLE)
