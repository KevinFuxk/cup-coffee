"""
providers_historical.py — point-in-time universe for the BACKTEST
=================================================================
Reconstructs the watchlist for any PAST trading day from Polygon's grouped-daily
endpoint (one call returns every US stock's OHLCV for that date, delisted names
INCLUDED — which is what makes the backtest survivorship-correct).

Efficient pattern: grouped-daily gives the whole market cheaply; we pre-filter to
gappers, then enrich only those survivors (a few dozen) with a details call for
type + shares. So a day costs ~2 grouped calls + a handful of details calls.

What's clean here: gap, price, liquidity, common-stock filter, delisted inclusion.
First-pass approximations (refine later):
  * market_cap = current shares_outstanding * that-day close (slightly off for
    stocks whose share count changed; fine for size-tiering).
  * revenue_growth_yoy defaults to None -> the EARNINGS branch won't fire, so this
    first backtest captures pure momentum gappers. Wire `revenue_fn` later to add
    earnings gappers with point-in-time revenue.

NOTE: Polygon field names below follow their documented API. If a response differs,
adjust the field names — verify against an actual call.

Runs in YOUR environment with your Polygon key. (No demo here — it needs network.)
"""

from __future__ import annotations

from datetime import date as Date, timedelta
from typing import Optional, Callable

from universe import SecuritySnapshot


class PolygonUniverseProvider:
    def __init__(self, api_key: str,
                 had_earnings_fn: Optional[Callable[[str, Date], bool]] = None,
                 energy_beta_fn: Optional[Callable[[str, Date, int], Optional[float]]] = None,
                 revenue_fn: Optional[Callable[[str, Date], Optional[float]]] = None,
                 min_gap_floor: float = 0.05,      # loosest tier gap; pre-filter only
                 min_price: float = 15.0,
                 min_dollar_volume: float = 20_000_000):
        self.key = api_key
        self._earn = had_earnings_fn or (lambda s, d: False)
        self._beta = energy_beta_fn or (lambda s, d, w: None)
        self._rev = revenue_fn                      # None -> earnings branch stays off
        self.min_gap_floor = min_gap_floor
        self.min_price = min_price
        self.min_dv = min_dollar_volume
        self._details_cache: dict = {}

    # --- Polygon calls ---
    def _grouped(self, day: Date) -> dict:
        import requests
        url = (f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/"
               f"{day.isoformat()}")
        r = requests.get(url, params={"adjusted": "true", "apiKey": self.key}, timeout=60)
        return {row["T"]: row for row in (r.json().get("results") or [])}

    def _prev_trading_grouped(self, day: Date) -> dict:
        d = day - timedelta(days=1)
        for _ in range(6):                          # step back over weekend/holidays
            g = self._grouped(d)
            if g:
                return g
            d -= timedelta(days=1)
        return {}

    def _details(self, ticker: str) -> dict:
        if ticker in self._details_cache:
            return self._details_cache[ticker]
        import requests
        url = f"https://api.polygon.io/v3/reference/tickers/{ticker}"
        res = requests.get(url, params={"apiKey": self.key}, timeout=30).json().get("results") or {}
        self._details_cache[ticker] = res
        return res

    # --- the interface the builder calls ---
    def universe_snapshot(self, as_of: Date) -> list[SecuritySnapshot]:
        today = self._grouped(as_of)
        prev = self._prev_trading_grouped(as_of)
        snaps: list[SecuritySnapshot] = []

        for tkr, row in today.items():
            close = row.get("c"); openp = row.get("o"); vol = row.get("v")
            prev_close = prev.get(tkr, {}).get("c")
            if not (close and openp and prev_close):
                continue
            gap = (openp - prev_close) / prev_close
            # cheap pre-filter (final filtering is the builder's job)
            if gap < self.min_gap_floor or close < self.min_price or close * vol < self.min_dv:
                continue

            det = self._details(tkr)                # enrich only survivors
            is_common = (det.get("type") == "CS")
            shares = (det.get("share_class_shares_outstanding")
                      or det.get("weighted_shares_outstanding"))
            market_cap = (shares * close) if shares else float(det.get("market_cap") or 0)
            sector = det.get("sic_description") or ""
            rev = self._rev(tkr, as_of) if self._rev else None

            snaps.append(SecuritySnapshot(
                symbol=tkr, close=float(close), market_cap=float(market_cap or 0),
                avg_dollar_volume=float(close) * float(vol), sector=sector,
                revenue_growth_yoy=rev, gap_pct=gap, is_common=is_common,
            ))
        return snaps

    def had_earnings(self, symbol: str, as_of: Date) -> bool:
        return self._earn(symbol, as_of)

    def energy_beta(self, symbol: str, as_of: Date, window_days: int) -> Optional[float]:
        return self._beta(symbol, as_of, window_days)
