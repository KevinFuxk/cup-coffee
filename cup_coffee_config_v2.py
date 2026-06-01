"""
CUP COFFEE — Strategy Configuration v2
=======================================
US equities + QQQ/SPY whitelist. 1 / 2 / 5-minute charts.
Event-driven daily universe (pre-market gapper scan), NOT a static list.

CORE IDEA: trade intraday cup-and-handle patterns that form AFTER a
momentum gap, plus the two index ETFs. Research target is EXIT TIMING —
which factors say hold to 3-4h vs take the 1h profit vs cut at breakeven.

BUILD ORDER:
  Stage 1  Daily universe scan   -> whitelist + qualifying gappers, per day
  Stage 2  Data layer            -> 1/2/5-min bars + earnings/revenue + gap
  Stage 3  Pattern labeler       -> detect cups + PATH-based outcome labels
  --- VALIDATE on known Tier 1 factors before continuing ---
  Stage 4  Feature compute       -> factor library (entry + mid-trade)
  Stage 5  Hypothesis loop       -> Claude proposes; code validates
"""

CONFIG = {

    # ============ STAGE 1: DAILY UNIVERSE (event-driven, size-tiered) ============
    "universe": {
        # Part A — permanent whitelist, exempt from the catalyst screen
        "whitelist": ["NASDAQ:QQQ", "AMEX:SPY"],

        # Part B — daily gapper scan (rebuilt EVERY trading day, point-in-time)
        # scan = the cheap base filters; the catalyst itself is size-relative below.
        "scan": {
            "markets": ["america"],
            "exchanges": ["NYSE", "NASDAQ"],
            "security_type": "common",        # excludes ETF/ETN/leveraged/crypto-vehicle/preferred
            "min_price": 15.0,                # applied point-in-time per day
            "min_dollar_volume": 20_000_000,  # liquidity floor
            # gap_pct = (today_open - prev_close_adjusted) / prev_close_adjusted
        },

        # SIZE-RELATIVE catalyst thresholds. A 5% gap on a mega-cap is a bigger
        # event than a 25% gap on a small-cap, so each tier has its own bar.
        # The "small" tier preserves your original spec (rev >=40%, gap >=10%/25%).
        "catalyst_by_size": {
            "mega":  {"earnings": {"rev_min": 0.10, "gap_min": 0.05}, "non_earnings": {"gap_min": 0.05}},
            "large": {"earnings": {"rev_min": 0.20, "gap_min": 0.07}, "non_earnings": {"gap_min": 0.10}},
            "mid":   {"earnings": {"rev_min": 0.30, "gap_min": 0.10}, "non_earnings": {"gap_min": 0.15}},
            "small": {"earnings": {"rev_min": 0.40, "gap_min": 0.10}, "non_earnings": {"gap_min": 0.25}},
            "micro": {"earnings": {"rev_min": 0.40, "gap_min": 0.10}, "non_earnings": {"gap_min": 0.25}},
        },

        # Trading-only exclusion (kept in RESEARCH universe per our discussion)
        "exclude_from_trading": {
            "energy_beta_above": 0.5,         # exclude high energy-beta names (chemicals/materials proxies) from LIVE trading only
        },
        # NOTE: mega-caps that never gap (GOOGL etc.) simply fail the scan — intended.
    },

    # ============ STAGE 1b: ENERGY BETA (tag for research, gate for trading) ============
    "energy_beta": {
        "benchmark": "AMEX:USO",
        "window_days": 60,
        "mode": "tag_and_trade_gate",         # tag every name; use as live-trading exclusion above
    },

    # ============ STAGE 2: DATA ============
    "data": {
        "timeframes": ["1min", "2min", "5min"],   # detector runs on all three
        "session": "RTH",                     # 09:30-16:00 ET only
        "history_start": "2024-01-01",        # 1yr to prototype; extend after validation
        "history_end":   "2024-12-31",
        "adjustment": "split_div",            # adjusted for analysis; keep raw for execution
        "include_delisted": True,             # CRITICAL — survivorship correctness
        "max_gap_bars": 3,
        "drop_halt_bars": True,
        # New dependencies for the catalyst screen:
        "earnings_calendar": True,            # point-in-time earnings dates (LSEG / FMP)
        "revenue_pit": True,                  # as-reported revenue YoY growth (NOT restated)
        "premarket_for_gap": True,            # need prev close + today open to compute the gap
    },

    # ============ STAGE 3: PATTERN DETECTION ============
    # Depth in ATR units (auto-scales across names); geometry from your spec.
    "pattern": {
        "atr_period": 14,                     # ATR on the active timeframe
        "cup_min_bars": 15,                   # YOUR spec: cup >= 15 bars (per timeframe)
        "cup_max_bars": 120,
        "cup_depth_min_atr": 1.5,             # cup >= 1.5 ATR deep
        "cup_depth_max_atr": 6.0,             # cup <= 6.0 ATR deep
        "rim_tolerance_atr": 0.5,             # right rim within 0.5 ATR of left rim
        "handle_min_bars": 4,                 # YOUR spec: handle >= 4 bars
        "handle_max_bars": 30,
        # YOUR spec: cup-depth : handle-depth ratio >= 4:1, ideal 5:1
        "handle_max_depth_frac": 0.25,        # 4:1  -> handle <= 25% of cup depth (minimum quality)
        "handle_ideal_depth_frac": 0.20,      # 5:1  -> handle <= 20% of cup depth (best)
        "breakout_buffer_atr": 0.1,           # close must clear rim by >= 0.1 ATR
        "lip_diff_max_frac_of_depth": 0.25,   # RULE 1: |left lip - right lip| <= 25% of cup depth
        "max_handles_per_cup": 3,             # RULE 2: one cup can spawn multiple handles
        "require_prior_trend": True,          # continuation pattern (the gap provides the trend)
        "prior_trend_lookback": 30,
        "prior_trend_min_atr": 2.0,
    },

    # ============ STAGE 3b: OUTCOME LABELING (path-based) ============
    # Structural stop from your spec + PATH capture for the exit-timing research.
    "labeling": {
        "entry": "breakout_close",            # enter at close of confirmed breakout bar
        "stop": "handle_low",                 # YOUR spec: lowest low between left & right lip of the handle
        "target": "measured_move",            # cup height projected up from the breakout (primary target)
        # R (risk unit) = entry_price - handle_low.  All P/L expressed in R.

        # PATH capture — this is what powers the "hold 1h vs 3h vs cut" research:
        "hold_checkpoints_min": [60, 120, 180, 240],   # record P/L (in R) at 1/2/3/4 hours
        "max_hold_min": 240,                  # 4-hour hard cap (clock time)
        "max_hold_bars": 240,                 # detector hold cap in BARS (=240 on 1-min; scale per timeframe)
        "record_mfe": True,                   # max favorable excursion (in R) and the minute it occurred
        "record_mae": True,                   # max adverse excursion
        "slippage_bps": 2,
        "commission_bps": 0.5,
    },

    # ============ BASELINE (benchmark, NOT a trade gate) ============
    # Clarified: 0.60 is the pattern's backtested BASE-RATE WIN RATE (~60% of
    # cup-handles hit target before stop). It is NOT a per-trade filter.
    # It is the benchmark the factor research must BEAT — a factor earns its
    # place only if it shifts the CONDITIONAL win rate significantly off 0.60.
    "baseline": {
        "win_rate": 0.60,                     # backtested base rate of the cup-handle pattern
        "use_as": "lift_benchmark",           # measure factor lift vs this; do NOT gate trades on it
    },

    # ============ STAGE 4: VALIDATION GATES (pure code, no LLM) ============
    "validation": {
        "min_events": 200,
        "ic_metric": "spearman",
        "multiple_testing": "benjamini_hochberg",
        "alpha": 0.05,
        "use_deflated_sharpe": True,
        "cv_method": "purged_walkforward",
        "n_folds": 5,
        "embargo_min": 240,                   # = max_hold_min, purge label overlap at fold edges
        "oos_fraction": 0.30,                 # untouched holdout
        "winrate_vs_baseline_test": "binomial_p0.60",  # is the subset's win rate really != 0.60?
    },

    # ============ THE RESEARCH TARGET (exit timing) ============
    # Two questions the factor loop must answer, conditional on being in a trade:
    "research_target": {
        "primary": "hold_extension",
        # Q1: which mid-trade factors predict MFE occurs AFTER 60 min
        #     (i.e. holding to 2-4h beats taking the 1h profit)?
        # Q2: which factors predict the trade will time out flat/negative
        #     (i.e. cut at breakeven rather than wait)?
        "label_q1": "mfe_after_60min",        # binary: did max favorable move happen past the 1h mark
        "label_q2": "timeout_no_progress",    # binary: flat/negative at vertical barrier
        "candidate_factor_tiers": ["tier0_regime", "tier1_setup", "tier2_microstructure", "intratrade"],
        # "intratrade" = factors that update DURING the hold: volume re-expansion,
        # market/sector strength shift, VWAP reclaim, new catalyst headline, etc.
    },

    # ============ PIPELINE SANITY CHECK ============
    "sanity_check": {
        "known_good_factors": ["breakout_volume_ratio", "handle_volume_dryup", "right_rim_recovery"],
        "min_expected_ic": 0.03,
        "eyeball_n_patterns": 20,             # LOOK at 20 detected patterns before trusting the detector
    },
}
