"""
data_layer.py — CUP COFFEE Stage 2: Intraday Data Layer
=======================================================
For each (symbol, qualifying_date) on the watchlist, pull the 1 / 2 / 5-minute
bars for that day, clean them, and hand a validated bundle to the pattern
detector (Stage 3).

What "clean" means here:
  * regular trading hours only (09:30-16:00 ET)
  * split/dividend adjusted
  * enough bars to even contain a pattern (short/halted days are dropped)
  * no large data gaps; halt/zero-volume bars removed
Each day that fails validation is dropped with a recorded reason — better to
trade nothing than to mine garbage.

Provider-agnostic, same as Stage 1: swap the synthetic feed for Polygon / LSEG
without touching the validation logic.

Runs as-is: `python data_layer.py` synthesizes a day that contains a rough
cup-and-handle (so Stage 3 will have something real to find) and loads it.
"""

from __future__ import annotations

import math
import random
import logging
from dataclasses import dataclass, field
from datetime import date as Date, datetime, time
from typing import Optional, Protocol

logger = logging.getLogger("cupcoffee.data")

RTH_BARS_1MIN = 390   # 09:30-16:00 inclusive of open minute


# ----------------------------------------------------------------------------
# Data contracts
# ----------------------------------------------------------------------------

@dataclass
class Bars:
    """The ATOM of the data layer — ONE symbol, ONE day, ONE timeframe, stored as six
    PARALLEL lists: bar i is (ts[i], o[i], h[i], l[i], c[i], v[i]), all the same length.
    Plain lists, NOT pandas, on purpose — the detector indexes these millions of times and
    `b.h[i]` must be instant (a pandas `.iloc[i]` would be ~100x slower). Rule of thumb:
    pandas at the edges (analysis/reporting), plain arrays in the hot core."""
    symbol: str
    date: Date
    timeframe: str                 # "1min" | "2min" | "5min"
    ts: list[datetime]
    o: list[float]; h: list[float]; l: list[float]; c: list[float]; v: list[float]
    adjusted: bool = True
    def __len__(self) -> int: return len(self.c)


@dataclass
class IntradaySeries:
    """All requested timeframes for one (symbol, date), validated. -> Stage 3."""
    symbol: str
    date: Date
    bars: dict[str, Bars]
    quality_flags: list[str] = field(default_factory=list)
    @property
    def ok(self) -> bool: return len(self.bars) > 0


# ----------------------------------------------------------------------------
# Provider interface
# ----------------------------------------------------------------------------

class BarDataProvider(Protocol):
    def intraday_bars(self, symbol: str, day: Date, timeframe: str) -> Optional[Bars]:
        """RTH, split/div-adjusted bars for one symbol/day/timeframe. None if
        unavailable. For BACKTEST this must serve delisted names too."""
        ...


# ----------------------------------------------------------------------------
# The data layer — validation lives here, not in the provider
# ----------------------------------------------------------------------------

class DataLayer:
    def __init__(self, config: dict, provider: BarDataProvider):
        self.cfg = config["data"]
        self.provider = provider
        self.min_bars = config.get("data", {}).get("min_bars", 60)

    def load(self, symbol: str, day: Date) -> Optional[IntradaySeries]:
        """Gatekeeper loop: for each timeframe, ask the provider for bars, VALIDATE them,
        keep only the clean ones. If nothing survives, drop the whole day (return None) with
        a recorded reason. Validation lives HERE, not in the provider — so swapping the data
        source (synthetic / Polygon / cached) never changes the quality bar."""
        bundle: dict[str, Bars] = {}
        flags: list[str] = []
        for tf in self.cfg["timeframes"]:
            bars = self.provider.intraday_bars(symbol, day, tf)
            if bars is None:
                flags.append(f"{tf}:missing")
                continue
            problem = self._validate(bars)
            if problem:
                flags.append(f"{tf}:{problem}")
                continue
            bundle[tf] = bars
        if not bundle:
            logger.info("DROP %s %s -> %s", symbol, day, flags)
            return None
        return IntradaySeries(symbol=symbol, date=day, bars=bundle, quality_flags=flags)

    def _validate(self, bars: Bars) -> Optional[str]:
        """The bouncer — returns a REASON string to reject the day, or None if clean.
        Three checks (better to trade nothing than mine garbage): too few bars (can't hold
        a pattern), too many zero-volume bars (halts), and bad ticks (high < low or a
        non-positive price = corrupt data)."""
        if len(bars) < self.min_bars:
            return f"too_few_bars({len(bars)})"
        # drop halt / zero-volume bars; if too many, reject the day
        zero_vol = sum(1 for vol in bars.v if vol <= 0)
        if zero_vol > self.cfg.get("max_gap_bars", 3):
            return f"too_many_zero_vol({zero_vol})"
        # crude bad-tick guard: any bar high<low or non-positive price
        for i in range(len(bars)):
            if bars.h[i] < bars.l[i] or bars.c[i] <= 0:
                return f"bad_tick@{i}"
        return None

    def load_watchlist(self, names: list[tuple[str, Date]]) -> list[IntradaySeries]:
        out = []
        for sym, day in names:
            s = self.load(sym, day)
            if s is not None:
                out.append(s)
        logger.info("Loaded %d/%d days cleanly", len(out), len(names))
        return out


# ----------------------------------------------------------------------------
# Downsampling helper (1-min -> 2/5-min). Providers can reuse this.
# ----------------------------------------------------------------------------

def downsample(one_min: Bars, factor: int, timeframe: str) -> Bars:
    """Collapse every `factor` one-minute bars into one bar — the universal candle rule:
    open = FIRST bar's open, high = MAX high of the group, low = MIN low, close = LAST
    bar's close, volume = SUM. So a single 1-min fetch feeds 2- and 5-min too.
    e.g. AVXL: 389 one-min bars -> 78 five-min bars (factor=5)."""
    o=[]; h=[]; l=[]; c=[]; v=[]; ts=[]
    for i in range(0, len(one_min), factor):
        chunk = slice(i, i + factor)
        hs = one_min.h[chunk]; ls = one_min.l[chunk]; vs = one_min.v[chunk]
        if not one_min.c[chunk]:
            break
        o.append(one_min.o[i]); h.append(max(hs)); l.append(min(ls))
        c.append(one_min.c[chunk][-1]); v.append(sum(vs)); ts.append(one_min.ts[i])
    return Bars(one_min.symbol, one_min.date, timeframe, ts, o, h, l, c, v, one_min.adjusted)


# ----------------------------------------------------------------------------
# Provider stubs to wire later
# ----------------------------------------------------------------------------
# NOTE: these two are UNUSED interface sketches (same pattern as the old universe.py
# stubs). The LIVE bar source is research_data.CachedBarProvider -> ResearchData.bars(),
# which does the regular-hours filter and calls downsample() above. They remain only as a
# template for wiring a different data vendor.

class PolygonProvider:
    """Wire: GET /v2/aggs/ticker/{sym}/range/1/minute/{day}/{day}?adjusted=true
    Filter to RTH, then use downsample() for 2/5-min. Includes delisted tickers."""
    def intraday_bars(self, symbol, day, timeframe): raise NotImplementedError

class LSEGProvider:
    """Wire your LSEG connector's intraday endpoint; same RTH + adjust + downsample."""
    def intraday_bars(self, symbol, day, timeframe): raise NotImplementedError


# ----------------------------------------------------------------------------
# Synthetic provider — builds a day containing a rough cup-and-handle
# ----------------------------------------------------------------------------

class _SyntheticProvider:
    def __init__(self, seed: int = 7): self.seed = seed

    def _one_min(self, symbol: str, day: Date) -> Bars:
        rnd = random.Random(self.seed)
        px = 100.0
        o=[]; h=[]; l=[]; c=[]; v=[]; ts=[]
        base_open = datetime.combine(day, time(9, 30))
        for i in range(RTH_BARS_1MIN):
            # shape: prior trend -> cup -> handle -> breakout -> drift
            if   i < 40:   drift =  0.12           # prior up-trend
            elif i < 70:   drift = -0.18           # cup down-leg
            elif i < 100:  drift =  0.18           # cup up-leg (recovery)
            elif i < 112:  drift = -0.10           # handle (shallow pullback)
            elif i < 130:  drift =  0.30           # breakout
            else:          drift =  0.02           # post-breakout drift
            px = max(1.0, px + drift + rnd.uniform(-0.15, 0.15))
            op = px + rnd.uniform(-0.05, 0.05)
            cl = px + rnd.uniform(-0.05, 0.05)
            hi = max(op, cl) + abs(rnd.uniform(0, 0.12))
            lo = min(op, cl) - abs(rnd.uniform(0, 0.12))
            # volume: open spike, decline into cup, dry handle, breakout spike
            if   i < 10:   vol = 30000
            elif i < 70:   vol = 16000 - i * 120
            elif i < 100:  vol = 8000 + (i - 70) * 150
            elif i < 112:  vol = 4000             # handle dry-up
            elif i < 130:  vol = 26000            # breakout surge
            else:          vol = 9000
            vol = int(max(500, vol + rnd.uniform(-1500, 1500)))
            o.append(round(op,2)); h.append(round(hi,2)); l.append(round(lo,2))
            c.append(round(cl,2)); v.append(vol)
            ts.append(base_open.replace(minute=(9*60+30+i)%60, hour=9+(30+i)//60))
        return Bars(symbol, day, "1min", ts, o, h, l, c, v, adjusted=True)

    def intraday_bars(self, symbol, day, timeframe):
        one = self._one_min(symbol, day)
        if timeframe == "1min": return one
        if timeframe == "2min": return downsample(one, 2, "2min")
        if timeframe == "5min": return downsample(one, 5, "5min")
        return None


# ----------------------------------------------------------------------------
# Demo
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = {"data": {
        "timeframes": ["1min", "2min", "5min"],
        "session": "RTH", "adjustment": "split_div",
        "max_gap_bars": 3, "min_bars": 60,
    }}

    layer = DataLayer(cfg, _SyntheticProvider())
    # pretend these came off the Stage 1 watchlist:
    watch = [("DELL", Date(2026, 5, 29)), ("ASTC", Date(2026, 5, 29))]
    series = layer.load_watchlist(watch)

    print("\n=== LOADED SERIES (ready for Stage 3) ===")
    for s in series:
        print(f"\n{s.symbol}  {s.date}   flags={s.quality_flags or 'none'}")
        for tf, b in s.bars.items():
            rng_lo, rng_hi = min(b.l), max(b.h)
            print(f"  {tf:<5} bars={len(b):<4} "
                  f"open={b.o[0]:.2f} close={b.c[-1]:.2f} "
                  f"day_range=[{rng_lo:.2f}, {rng_hi:.2f}] "
                  f"vol_total={sum(b.v):,}")
