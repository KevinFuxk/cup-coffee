"""
feature_library.py — the diverse, point-in-time factor library
===============================================================
For each detected cup-and-handle trade, compute a broad, deliberately UNBIASED
spread of candidate factors — so the scorer, not our prior, decides what matters.
Categories (each prefixed in the factor name):

  tech_*   intraday + daily price/volume structure   (Polygon bars)
  macro_*  market regime / relative strength / vol    (SPY + USO daily)
  fund_*   fundamentals, point-in-time                (Polygon vX financials)
  news_*   coverage + sentiment before the trade      (Polygon news)
  cal_*    calendar / seasonality                     (date only)
  noise_*  reproducible random CONTROLS — must NOT pass; if they do, our
           significance bar is too low (a built-in honesty check)

POINT-IN-TIME DISCIPLINE: every factor uses only information available at or
before the breakout bar. Daily/fundamental/news use a strict "before the trade
day" filter; intraday uses only bars up to breakout_idx. No future leaks in.

Missing data -> the factor is None for that trade, and the scorer simply drops
those trades for that one factor (scores on whatever subset has it).
"""

from __future__ import annotations
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)

import math
import hashlib
from datetime import date as Date, datetime, timezone, timedelta
from typing import Optional

from research_data import ResearchData

ET_NULL = None


def _safe_div(a, b):
    return a / b if (b not in (0, None) and a is not None) else None

def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None

def _row_date(r) -> Date:
    return datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).date()

def _atr_upto(b, upto: int) -> Optional[float]:
    trs = []
    for i in range(1, min(upto, len(b))):
        trs.append(max(b.h[i]-b.l[i], abs(b.h[i]-b.c[i-1]), abs(b.l[i]-b.c[i-1])))
    return sum(trs)/len(trs) if trs else None

def _returns(closes):
    return [closes[i]/closes[i-1]-1 for i in range(1, len(closes)) if closes[i-1]]


class FeatureLibrary:
    # factor name -> category, for reporting
    CATEGORIES = {
        "tech": "technical", "macro": "macro/regime", "fund": "fundamental",
        "news": "news/sentiment", "cal": "calendar", "noise": "control",
    }

    def __init__(self, rd: ResearchData):
        self.rd = rd

    def category_of(self, name: str) -> str:
        return self.CATEGORIES.get(name.split("_", 1)[0], "other")

    # ------------------------------------------------------------------
    def compute(self, ev: dict) -> dict:
        sym = ev["symbol"]
        day = Date.fromisoformat(ev["day"])
        f: dict[str, Optional[float]] = {}

        # ---- data sources (all cached) ----
        b = self.rd.bars(sym, day, ev["timeframe"])
        d_sym = self.rd.daily(sym, day - timedelta(days=160), day)
        d_spy = self.rd.daily("SPY", day - timedelta(days=120), day)
        d_uso = self.rd.daily("USO", day - timedelta(days=120), day)
        news = self.rd.news(sym, day, lookback_days=7)
        fins = self.rd.financials(sym, day)
        det = self.rd.details(sym)

        self._tech_intraday(f, ev, b)
        self._tech_daily(f, ev, day, d_sym)
        self._macro(f, day, d_sym, d_spy, d_uso)
        self._fundamental(f, day, det, fins, d_sym)
        self._news(f, news, sym)
        self._calendar(f, day)
        self._noise(f, ev)
        return f

    # ---- TECHNICAL: intraday structure (only bars up to breakout) ----
    def _tech_intraday(self, f, ev, b):
        if b is None or len(b) == 0:
            for k in ("tech_breakout_vol_ratio","tech_handle_vol_dryup","tech_rim_recovery",
                      "tech_cup_depth_atr","tech_vwap_dist_at_breakout","tech_breakout_time_frac",
                      "tech_pre_breakout_run"):
                f[k] = None
            return
        bi = min(ev["breakout_idx"], len(b)-1)
        cl, cr = ev["cup_left_idx"], ev["cup_right_idx"]
        prior = b.v[max(0, bi-20):bi] or [0]
        f["tech_breakout_vol_ratio"] = _safe_div(b.v[bi], _mean(prior))
        cupv = _mean(b.v[cl:cr]); handv = _mean(b.v[cr:bi])
        f["tech_handle_vol_dryup"] = _safe_div(handv, cupv)
        f["tech_rim_recovery"] = _safe_div(b.h[cr], b.h[cl]) if cl < len(b) and cr < len(b) else None
        atr = _atr_upto(b, bi)
        f["tech_cup_depth_atr"] = _safe_div(ev.get("cup_depth"), atr)
        vol = sum(b.v[:bi+1]); vwap = (sum(b.c[i]*b.v[i] for i in range(bi+1))/vol) if vol else None
        f["tech_vwap_dist_at_breakout"] = (ev["entry_price"]/vwap - 1) if vwap else None
        f["tech_breakout_time_frac"] = bi/len(b)
        f["tech_pre_breakout_run"] = (b.c[bi]/b.c[0]-1) if b.c[0] else None

    # ---- TECHNICAL: prior-day structure + the gap (point-in-time) ----
    def _tech_daily(self, f, ev, day, d_sym):
        for k in ("tech_gap_pct","tech_prior_5d_return","tech_dist_from_20d_high",
                  "tech_daily_atr_pct","tech_log_dollar_vol","tech_risk_to_cup"):
            f[k] = None
        f["tech_risk_to_cup"] = _safe_div(ev.get("risk_R"), ev.get("cup_depth"))
        prior = [r for r in d_sym if _row_date(r) < day]
        dayrow = [r for r in d_sym if _row_date(r) == day]
        if len(prior) >= 6:
            pc = prior[-1]["c"]
            f["tech_prior_5d_return"] = (pc/prior[-6]["c"]-1) if prior[-6]["c"] else None
            hi20 = max(r["h"] for r in prior[-20:])
            f["tech_dist_from_20d_high"] = (pc/hi20-1) if hi20 else None
            trs = [max(prior[i]["h"]-prior[i]["l"],
                       abs(prior[i]["h"]-prior[i-1]["c"]),
                       abs(prior[i]["l"]-prior[i-1]["c"])) for i in range(max(1,len(prior)-20), len(prior))]
            f["tech_daily_atr_pct"] = _safe_div(_mean(trs), pc)
            f["tech_log_dollar_vol"] = math.log(max(1.0, pc*prior[-1]["v"]))
            if dayrow:
                f["tech_gap_pct"] = (dayrow[0]["o"]/pc - 1) if pc else None

    # ---- MACRO / REGIME ----
    def _macro(self, f, day, d_sym, d_spy, d_uso):
        for k in ("macro_spy_5d_return","macro_spy_above_20dma","macro_spy_realized_vol",
                  "macro_relative_strength_5d","macro_energy_beta"):
            f[k] = None
        spy = [r for r in d_spy if _row_date(r) < day]
        if len(spy) >= 21:
            sc = spy[-1]["c"]
            spy5 = (sc/spy[-6]["c"]-1) if spy[-6]["c"] else None
            f["macro_spy_5d_return"] = spy5
            ma20 = _mean([r["c"] for r in spy[-20:]])
            f["macro_spy_above_20dma"] = float(sc > ma20) if ma20 else None
            rets = _returns([r["c"] for r in spy[-21:]])
            if rets:
                mu = _mean(rets); var = _mean([(x-mu)**2 for x in rets])
                f["macro_spy_realized_vol"] = math.sqrt(var)*math.sqrt(252) if var is not None else None
            sym_prior = [r for r in d_sym if _row_date(r) < day]
            if len(sym_prior) >= 6 and spy5 is not None and sym_prior[-6]["c"]:
                f["macro_relative_strength_5d"] = (sym_prior[-1]["c"]/sym_prior[-6]["c"]-1) - spy5
        # energy beta: stock vs USO daily returns, ~60d before the day
        uso = [r for r in d_uso if _row_date(r) < day]
        sym_p = [r for r in d_sym if _row_date(r) < day]
        n = min(len(uso), len(sym_p), 61)
        if n >= 15:
            sr = _returns([r["c"] for r in sym_p[-n:]])
            br = _returns([r["c"] for r in uso[-n:]])
            m = min(len(sr), len(br)); sr, br = sr[-m:], br[-m:]
            mb = _mean(br); ms = _mean(sr)
            cov = sum((sr[i]-ms)*(br[i]-mb) for i in range(m))
            var = sum((x-mb)**2 for x in br)
            f["macro_energy_beta"] = cov/var if var else None

    # ---- FUNDAMENTAL (point-in-time) ----
    def _fundamental(self, f, day, det, fins, d_sym):
        for k in ("fund_revenue_growth_yoy","fund_gross_margin","fund_net_margin","fund_log_market_cap"):
            f[k] = None
        # market cap from details (shares) * latest prior close
        prior = [r for r in d_sym if _row_date(r) < day]
        shares = det.get("share_class_shares_outstanding") or det.get("weighted_shares_outstanding")
        if shares and prior:
            f["fund_log_market_cap"] = math.log(max(1.0, shares*prior[-1]["c"]))
        elif det.get("market_cap"):
            f["fund_log_market_cap"] = math.log(max(1.0, det["market_cap"]))
        if not fins:
            return
        def inc(r): return r.get("financials", {}).get("income_statement", {})
        def val(r, k): return inc(r).get(k, {}).get("value")
        latest = fins[0]
        rev0 = val(latest, "revenues")
        f["fund_gross_margin"] = _safe_div(val(latest, "gross_profit"), rev0)
        f["fund_net_margin"] = _safe_div(val(latest, "net_income_loss"), rev0)
        # YoY: same fiscal_period one year earlier
        fp, fy = latest.get("fiscal_period"), latest.get("fiscal_year")
        try:
            fy_prev = str(int(fy) - 1)
            yoy = next((r for r in fins if r.get("fiscal_period") == fp and r.get("fiscal_year") == fy_prev), None)
            if yoy and rev0:
                rprev = val(yoy, "revenues")
                f["fund_revenue_growth_yoy"] = (rev0/rprev - 1) if rprev else None
        except (TypeError, ValueError):
            pass

    # ---- NEWS / SENTIMENT ----
    def _news(self, f, news, sym):
        s = sym.split(":")[-1]
        f["news_count_7d"] = float(len(news))
        sents = []
        for a in news:
            for ins in (a.get("insights") or []):
                if ins.get("ticker") == s:
                    sents.append({"positive": 1.0, "negative": -1.0}.get(ins.get("sentiment"), 0.0))
        f["news_sentiment_7d"] = _mean(sents) if sents else (0.0 if news else None)

    # ---- CALENDAR ----
    def _calendar(self, f, day):
        f["cal_day_of_week"] = float(day.weekday())
        f["cal_month"] = float(day.month)

    # ---- NOISE CONTROLS (reproducible) ----
    def _noise(self, f, ev):
        for tag in ("1", "2"):
            h = hashlib.md5(f"{ev['symbol']}{ev['day']}{ev['timeframe']}{ev.get('breakout_idx')}{tag}".encode()).hexdigest()
            f[f"noise_{tag}"] = int(h[:8], 16) / 0xFFFFFFFF

    # convenience: the full ordered factor name list (from one computed dict)
    @staticmethod
    def names(sample: dict) -> list:
        return list(sample.keys())


if __name__ == "__main__":
    import os, json
    rd = ResearchData(os.environ["POLYGON_API_KEY"])
    lib = FeatureLibrary(rd)
    # try on the first few real events
    evs = [json.loads(l) for l in open("data/events.jsonl")][:4]
    for ev in evs:
        f = lib.compute(ev)
        present = sum(1 for v in f.values() if v is not None)
        print(f"\n{ev['symbol']:<9} {ev['day']} {ev['timeframe']}  ({present}/{len(f)} factors present)")
        for k, v in f.items():
            print(f"   {k:<28} {('%.4f'%v) if isinstance(v,(int,float)) else v}")
