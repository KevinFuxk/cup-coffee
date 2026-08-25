"""
universe.py — CUP COFFEE Stage 1: Daily Universe Builder (size-tiered)
=====================================================================
Produces the daily watchlist AND tags every name with three categories so the
factor research can condition on them:

    size_tier       mega / large / mid / small / micro   (by market cap)
    return_bucket    by gap magnitude
    earnings_bucket  no_earnings / rev_negative / rev_0_20 / rev_20_40 / rev_40+

Catalyst thresholds are SIZE-RELATIVE — a 5% gap on a mega-cap is a bigger
event than a 25% gap on a small-cap, so giants (e.g. DELL) enter the database
for more data without diluting the small-cap calibration.

Including giants for DATA is separate from trading them the same way: the
size_tier tag keeps every regime separable downstream.

Runs as-is: `python universe.py` uses a FakeProvider seeded with five real
names (now with illustrative market caps) to show the tiered funnel.
"""

from __future__ import annotations
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)

import logging
from dataclasses import dataclass
from datetime import date as Date
from typing import Optional, Protocol

logger = logging.getLogger("cupcoffee.universe")


# ----------------------------------------------------------------------------
# Size tiers + category classifiers (the conditioning tags)
# ----------------------------------------------------------------------------

SIZE_TIERS = [           # (tier, min market cap USD), evaluated high -> low
    ("mega",  200e9),
    ("large",  10e9),
    ("mid",     2e9),
    ("small", 300e6),
    ("micro",     0),
]

def classify_size(market_cap: Optional[float]) -> str:
    if market_cap is None:
        return "unknown"
    for tier, floor in SIZE_TIERS:
        if market_cap >= floor:
            return tier
    return "micro"

def return_bucket(gap: Optional[float]) -> str:
    if gap is None:            return "na"
    if gap >= 0.50:            return "gap_50+"
    if gap >= 0.25:            return "gap_25_50"
    if gap >= 0.10:            return "gap_10_25"
    if gap >= 0.05:            return "gap_5_10"
    return "gap_lt_5"

def earnings_bucket(had_earnings: bool, rev: Optional[float]) -> str:
    if not had_earnings:       return "no_earnings"
    if rev is None:            return "earnings_rev_unknown"
    if rev >= 0.40:            return "rev_40+"
    if rev >= 0.20:            return "rev_20_40"
    if rev >= 0.0:             return "rev_0_20"
    return "rev_negative"


# ----------------------------------------------------------------------------
# Data contracts
# ----------------------------------------------------------------------------

@dataclass
class SecuritySnapshot:
    symbol: str
    close: float
    market_cap: float                  # for size tiering
    avg_dollar_volume: float
    sector: str
    revenue_growth_yoy: Optional[float]
    gap_pct: Optional[float]
    is_common: bool


@dataclass
class Candidate:
    symbol: str
    reason: str                        # "whitelist" | "earnings_gap" | "momentum_gap"
    price: float
    market_cap: Optional[float]
    size_tier: str
    return_bucket: str
    earnings_bucket: str
    gap_pct: Optional[float]
    revenue_growth_yoy: Optional[float]
    energy_beta: Optional[float]
    tradable: bool
    notes: str = ""


@dataclass
class Watchlist:
    as_of: Date
    candidates: list[Candidate]
    funnel: dict

    def tradable(self) -> list[Candidate]:
        return [c for c in self.candidates if c.tradable]

    def research(self) -> list[Candidate]:
        return list(self.candidates)


# ----------------------------------------------------------------------------
# Provider interface
# ----------------------------------------------------------------------------

class DataProvider(Protocol):
    def universe_snapshot(self, as_of: Date) -> list[SecuritySnapshot]: ...
    def had_earnings(self, symbol: str, as_of: Date) -> bool: ...
    def energy_beta(self, symbol: str, as_of: Date, window_days: int) -> Optional[float]: ...


# ----------------------------------------------------------------------------
# Builder — size-tiered catalyst logic
# ----------------------------------------------------------------------------

class DailyUniverseBuilder:
    def __init__(self, config: dict, provider: DataProvider):
        self.cfg = config["universe"]
        self.ebeta_cfg = config["energy_beta"]
        self.provider = provider

    def build(self, as_of: Date) -> Watchlist:
        funnel: dict[str, int] = {}
        candidates: list[Candidate] = []
        scan = self.cfg["scan"]
        by_size = self.cfg["catalyst_by_size"]

        # Part A: whitelist
        for sym in self.cfg["whitelist"]:
            candidates.append(Candidate(
                symbol=sym, reason="whitelist", price=float("nan"),
                market_cap=None, size_tier="index", return_bucket="na",
                earnings_bucket="na", gap_pct=None, revenue_growth_yoy=None,
                energy_beta=None, tradable=True, notes="index ETF whitelist",
            ))
        funnel["whitelist"] = len(candidates)

        # Part B: scan
        snaps = self.provider.universe_snapshot(as_of)
        funnel["snapshot_total"] = len(snaps)

        stage = [s for s in snaps if s.is_common]
        funnel["common_only"] = len(stage)
        stage = [s for s in stage if s.close >= scan["min_price"]]
        funnel["price_ge_floor"] = len(stage)
        stage = [s for s in stage if s.avg_dollar_volume >= scan["min_dollar_volume"]]
        funnel["liquidity_ok"] = len(stage)

        # catalyst test: per-size-tier momentum gap (usual cases) OR a flat
        # earnings-gapper rule (reported earnings + revenue growth >= 40% + gap >= 10%),
        # added as an extra net so important earnings names are never missed.
        earn = self.cfg.get("earnings_catalyst", {"rev_min": 0.40, "gap_min": 0.10})
        passed: list[tuple[SecuritySnapshot, str, str]] = []   # (snap, tier, reason)
        for s in stage:
            if s.gap_pct is None:
                continue
            tier = classify_size(s.market_cap)
            th = by_size.get(tier, by_size["small"])
            reason = None
            if s.gap_pct >= th["non_earnings"]["gap_min"]:
                reason = "momentum_gap"
            elif (s.revenue_growth_yoy is not None          # high-growth gapper (Polygon revenue, no FMP)
                  and s.revenue_growth_yoy >= earn["rev_min"]
                  and s.gap_pct >= earn["gap_min"]):
                reason = "earnings_gap"
            if reason:
                passed.append((s, tier, reason))
        funnel["catalyst_pass"] = len(passed)

        # energy-beta tag + trading gate; attach all three category tags
        beta_gate = self.cfg["exclude_from_trading"]["energy_beta_above"]
        window = self.ebeta_cfg["window_days"]
        for s, tier, reason in passed:
            had = self.provider.had_earnings(s.symbol, as_of)
            beta = self.provider.energy_beta(s.symbol, as_of, window)
            tradable = not (beta is not None and beta > beta_gate)
            candidates.append(Candidate(
                symbol=s.symbol, reason=reason, price=s.close, market_cap=s.market_cap,
                size_tier=tier, return_bucket=return_bucket(s.gap_pct),
                earnings_bucket=earnings_bucket(had, s.revenue_growth_yoy),
                gap_pct=s.gap_pct, revenue_growth_yoy=s.revenue_growth_yoy,
                energy_beta=beta, tradable=tradable,
                notes="" if tradable else f"research-only: energy_beta {beta:.2f} > {beta_gate}",
            ))

        wl = Watchlist(as_of=as_of, candidates=candidates, funnel=funnel)
        funnel["tradable_final"] = len(wl.tradable())
        logger.info("Universe %s funnel: %s", as_of, funnel)
        return wl


# ----------------------------------------------------------------------------
# Provider implementation lives in providers_historical.py
# ----------------------------------------------------------------------------
# The real, survivorship-correct backtest provider is `PolygonUniverseProvider`
# in providers_historical.py: it pulls Polygon grouped-daily (delisted names
# INCLUDED -> no survivorship bias) and feeds SecuritySnapshot rows into the
# funnel above. (Unused Live/Historical interface stubs were removed from here.)


# ----------------------------------------------------------------------------
# Runnable demo — five real names + illustrative market caps
# ----------------------------------------------------------------------------

class _FakeProvider:
    _DATA = [
        # symbol, close,   mktcap,   gap%,  rev_yoy, had_earn, e_beta, sector
        ("ASTC",  49.80,   0.18e9,  0.692,  0.116, False, 0.08, "Electronic Technology"),
        ("DELL", 420.91, 285.00e9,  0.328,  0.389, True,  0.21, "Electronic Technology"),
        ("OKTA", 123.27,  21.00e9,  0.301,  0.117, True,  0.05, "Technology Services"),
        ("NTAP", 174.29,  36.00e9,  0.224,  0.053, True,  0.12, "Electronic Technology"),
        ("TSSI",  16.48,   0.45e9,  0.217, -0.126, True,  0.04, "Technology Services"),
    ]
    def universe_snapshot(self, as_of):
        return [SecuritySnapshot(sym, close, mc, close*4_000_000, sec, rg, gap, True)
                for (sym, close, mc, gap, rg, _e, _b, sec) in self._DATA]
    def had_earnings(self, symbol, as_of):
        return {r[0]: r[5] for r in self._DATA}[symbol]
    def energy_beta(self, symbol, as_of, window_days):
        return {r[0]: r[6] for r in self._DATA}[symbol]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = {
        "universe": {
            "whitelist": ["NASDAQ:QQQ", "AMEX:SPY"],
            "scan": {"min_price": 15.0, "min_dollar_volume": 20_000_000},
            # SIZE-RELATIVE catalyst thresholds. Small tier == your original spec.
            "catalyst_by_size": {
                "mega":  {"earnings": {"rev_min": 0.10, "gap_min": 0.05}, "non_earnings": {"gap_min": 0.05}},
                "large": {"earnings": {"rev_min": 0.20, "gap_min": 0.07}, "non_earnings": {"gap_min": 0.10}},
                "mid":   {"earnings": {"rev_min": 0.30, "gap_min": 0.10}, "non_earnings": {"gap_min": 0.15}},
                "small": {"earnings": {"rev_min": 0.40, "gap_min": 0.10}, "non_earnings": {"gap_min": 0.25}},
                "micro": {"earnings": {"rev_min": 0.40, "gap_min": 0.10}, "non_earnings": {"gap_min": 0.25}},
            },
            "exclude_from_trading": {"energy_beta_above": 0.5},
        },
        "energy_beta": {"window_days": 60},
    }

    wl = DailyUniverseBuilder(cfg, _FakeProvider()).build(Date(2026, 5, 29))

    print("\n=== FUNNEL ===")
    for stage, n in wl.funnel.items():
        print(f"  {stage:<18} {n}")

    print("\n=== TRADABLE WATCHLIST (with category tags) ===")
    print(f"  {'symbol':<11}{'tier':<7}{'reason':<13}{'gap':>6}  {'return_bucket':<11}{'earnings_bucket'}")
    for c in wl.tradable():
        g = f"{c.gap_pct:+.0%}" if c.gap_pct is not None else "   -"
        print(f"  {c.symbol:<11}{c.size_tier:<7}{c.reason:<13}{g:>6}  {c.return_bucket:<11}{c.earnings_bucket}")

    print("\n=== EXCLUDED (tier-aware reason) ===")
    kept = {c.symbol for c in wl.candidates}
    fp = _FakeProvider()
    tiers = cfg["universe"]["catalyst_by_size"]
    for s in fp.universe_snapshot(None):
        if s.symbol in kept:
            continue
        tier = classify_size(s.market_cap)
        th = tiers[tier]["earnings"]
        if fp.had_earnings(s.symbol, None):
            why = f"{tier}: earnings, revYoY {s.revenue_growth_yoy:+.0%} < {th['rev_min']:.0%}"
        else:
            why = f"{tier}: no earnings, gap {s.gap_pct:+.0%} < threshold"
        print(f"  {s.symbol:<11}{why}")
