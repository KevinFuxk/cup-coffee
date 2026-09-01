# High Tight Flag — Daily Program Instructions (for the HTF chat channel)

*Written 2026-09-01 by the cup-and-handle channel, at the user's request. This is the
mission brief: make the 1-minute high-tight-flag strategy run the SAME daily machinery
the 15s cup-and-handle already runs — bot trading during the market, replay/record in
the afternoon, data output in `data/`. Read this file first, then build.*

## Context you must know (already done — do not redo)

- **THE PIVOT (2026-09-02):** the old research program is closed. The program is now
  two forward-tested strategies on a manually fed news-source universe:
  15-second cup-and-handle (the cup channel) and **1-minute high tight flag (you)**.
- **Your timeframe change is already implemented and pushed** (commits `749770f`,
  `a37aeee`): `pattern_detector_tightflag.py` CONFIG is `timeframe: "1min"`,
  `min_coverage: 1`, clock width is parameterized (`clock_bars`/`cfg_width`;
  `Clock5.WIDTH` in the live bot follows CONFIG). Setup = bars 09:30–09:31 and
  09:31–09:32; the resting stop-entry may trigger **from bar 3 until 09:45 ET, never
  after** (`entry_deadline_min: 15` — USER RULE 2026-09-02; it reproduces the old
  5-min bar-3-only rule exactly, so old piles stay comparable).
- **Strategy rules are FROZEN.** ratio ≥2.0, fly-trigger 1.75R, trail-from-bar-4 with
  2-bar lag, R = entry−stop, EOD 15:49 — none of it changes without the user saying so.
- The ledger was cleaned on 2026-09-01: **all recorded history starts 2026-08-28.**

## What to build (mirror the cup channel's machinery)

1. **Morning bot (armed paper, runs beside the user's manual trading).**
   `live_trader_tightflag.py` on 1-min bars, IB Gateway paper port 4002, account
   DUR156797. Reuse `live_trader_ibkr.read_watchlist` for the day's tickers (the
   TradingView export auto-discovery — case-insensitive `*daytrade*.txt`, newest wins).
   Do NOT write the watchlist archive — the cup bot owns `data/watchlists/`
   (single-writer). Use a **distinct `--client-id`** (cup uses 8, its replay 9,
   explain 51, cache 52, record 53 — take 18 for live, 19 for replay). Own-book
   accounting already exists on both bots, so sharing the account is safe.
   First 1-min morning: shadow for ~30 minutes, then restart armed.

2. **Afternoon record (one command, after ~16:20 ET).** A replay/record entry point
   that replays the completed session through the live logic and writes
   `data/replay_trades_tightflag.csv` in the SAME 14-column schema as the cup ledger
   (`session,variant,symbol,tf,entry_time,entry,trigger,stop,stop_pct,exit_time,
   exit_kind,exit,R,peak_R`), idempotent per session; regenerate
   `data/replay_trades_tightflag.html` (copy the cup dashboard's pattern, incl. the
   universe join below). `tf` = "1min", `variant` = "htf".

3. **Nightly bar cache.** Cache each watched symbol's completed 1-min session to
   `cache/ibkr1min_days/<SYM>/<YYYY-MM-DD>.json` (row schema
   `{"t":epoch_ms,"o","h","l","c","v"}` — same as `cache/ibkr15s/`; do NOT touch
   `cache/ibkr1min/` — that's the old SPY/QQQ research cache in a different schema).
   Template: `cache_15s.py`.

4. **History builder.** Mirror `record_day.py` as `record_day_tightflag.py`: read the
   shared source-tagged watchlist `data/watchlists/<day>.txt`, JOIN (read-only) the
   shared `data/universe_log.csv` for qualified/dropped verdicts — the cup channel's
   `record_day.py` owns writing it — run the HTF scan on qualified names, append
   ledger rows. First job: backfill **2026-08-28 and 2026-08-31** (both watchlists and
   universe rows already exist; IBKR serves the 1-min bars).

5. **Tests.** `tests/test_tightflag.py` in the style of `tests/test_cup_coffee.py`
   (plain-assert runner): the 09:45 boundary (09:44 touch fills, 09:45 refused),
   ratio gate, fly-trigger arming, trail lag, and a real-day golden reproduction.

## Shared files — ownership rules

| File | Owner | You |
|---|---|---|
| `data/watchlists/*.txt`, `data/universe_log.csv` | cup channel | read only |
| `data/replay_trades.csv` + `.html` | cup channel | never touch |
| `data/replay_trades_tightflag.csv` + `.html`, `cache/ibkr1min_days/` | **you** | write |
| `pattern_detector_tightflag.py`, `live_trader_tightflag.py`, `scan_tightflag.py`, `viz_tightflag.py` | **you** | edit freely |
| everything else (cup detector, config, live bot, tests/test_cup_coffee.py) | cup channel | read only — import yes, edit no |

## The daily runbook (what the user will actually run)

    # morning, after exporting the TradingView watchlist + starting Gateway (paper)
    python live_trader_tightflag.py --port 4002 --client-id 18 --arm   # + your flags
    # evening, after ~16:20 ET
    python <your record entry point>

## Non-negotiables (learned the hard way over here)

- **Never log into IBKR elsewhere while bots run** (session takeover = silent data death).
- **No arming after the day's EOD flatten** — we shipped exactly this bug; check your
  bot's post-15:49 path and add a regression test.
- **Nothing is silently dropped**: every skip/refusal prints a reason; disqualified
  tickers stay visible in the record.
- **Frozen rules + hypothesis before test**: any rule variant gets its expected outcome
  written down BEFORE running, and goes to the user for approval first.
- Commit with real messages to this repo (branch `strategy-v2-rebuild`), push when the
  user asks; data CSVs are gitignored — never rely on git to back them up.
