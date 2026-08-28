# What the Code Actually Does

*A ground-truth map of the Cup Coffee system: data flow, every parameter, every implicit
assumption. Written 2026-08-24 from the source itself, not from intentions. Where the config
file SAYS one thing and the code DOES another, this document records what the code does.
Every claim was adversarially verified against the source by independent review (104 claims
checked; 6 corrections applied, including one live-bot hole discovered in the process — §5).*

---

## 0. One paragraph

The system trades **intraday cup-and-handle breakouts, long-only, US stocks**, on 1/2/5-minute
charts. A cup is a 15–60 bar valley between two roughly-level peaks; the handle is a shallow
pause (≤20% of cup depth) under the right rim; entry is a resting **buy-stop at right-rim high
+ $0.01** filled on first touch, never earlier than the 4th handle bar; stop = handle low;
take-profit = **entry + 6R**; everything is flat by **15:49 ET**. One pending setup per symbol,
max 5 concurrent, risk **1% of equity** per trade, minimum stop distance **0.25% of price**.
The same frozen detector runs the backtest and the live bot.

---

## 1. The map — files and who calls whom

```
RESEARCH PATH (frozen historical data)
  cache/minute/*             Polygon 1-min bars, 6,773 files (gappers 2021-2026) — FROZEN, API lapsed
  cache/ibkr5/{SPY,QQQ}/     IBKR 5-min bars 2004-2026 (22 yr)
  cache/ibkr1min/{SPY,QQQ}   IBKR 1-min bars 2024-02..2026-07 (2.5 yr)
        │
        ▼
  research_data.py   ResearchData.bars() → data_layer.downsample() → Bars
        │
        ▼
  pattern_detector.py  PatternDetector.detect(signals_only=True)  ← THE strategy, one copy
        │
        ▼
  research_data.py   label_full_path()  → realized R at every TP 1R..20R + MFE/MAE
        │
        ▼
  data/events_*.jsonl piles → evaluation scripts (net-of-cost stats, TP sweep, null benchmark)
        │
        ▼
  app_trades.py      dashboard: metrics, cumulative R, MFE histogram, trade pictures

LIVE PATH (IBKR paper, account DUR156797)
  TradingView export (~/Downloads/*DayTrade*.txt)  ← THE actual live universe (manual)
        │
        ▼
  live_trader_ibkr.py  read_watchlist → IB Gateway :4002 → 1-min stream
        │                → MinuteAggregator (2min, 5min, clock-aligned from 09:30)
        ▼
  scan_setups()  (mirrors the detector gate-for-gate, exposes the "forming" stage)
        │
        ▼
  arm_pending → native IBKR bracket: BUY-STP parent + OCA(SELL-LMT tp / SELL-STP sl)
        │
        ▼
  fills → data/paper_fills.csv · board/logs → logs/live_YYYY-MM-DD.log · EOD 15:49 flatten

EVENING
  replay_record.py → live_trader --replay ×2 (minstop 0.25 / 0) → data/replay_trades.csv + .html
                   → explain_day.py (why every watched symbol did/didn't trade)

DISCIPLINE
  splits.py  Develop 2004-16 / Validate 2017-21 (2 looks) / Lockbox 2022-26 (1 look)
             for SPY/QQQ; access enforced in code, logged to data/splits/ACCESS_LOG.txt
```

One deliberate design fact: **`pattern_detector.py` is the only copy of the strategy.**
The live bot's `scan_setups()` re-implements the same walk to expose the pre-entry "forming"
state, and the frozen detector runs beside it as a referee (`📋 detector confirms entry`) —
divergence between the two is a bug by definition.

---

## 2. The pattern — every gate, in firing order, with exact values

All prices are bar **highs/lows**, never closes, unless stated. Bars are RTH only.

| # | Gate | Exact rule | Config key (value) |
|---|------|-----------|--------------------|
| 1 | **Left rim** | bar `li` is a local peak: `h[li] ≥ h[li-1]` **and** `h[li] ≥ h[li+1]` — ties count | (hard-coded `_is_peak`) |
| 2 | **Cup length** | right rim `ri` is 15–60 bars after `li` | `cup_min_bars` 15, `cup_max_bars` 60 |
| 3 | **Right rim is a peak** | same `_is_peak` test at `ri` | — |
| 4 | **Rim band** | `left_high − 0.25·depth ≤ right_high ≤ left_high + 0.25·depth`, where `depth = left_high − cup_low` | `right_rim_recovery_frac` 0.25 |
| 5 | **Rim line clean** | no interior bar's high pokes **strictly above** the straight line joining the two rim highs (tolerance 1e-9) | (hard-coded `_obstructed`) |
| 6 | **Cup bottom** | lowest low **strictly between** the rims (`li+1 .. ri−1`); rims themselves excluded | — |
| 7 | **Rim symmetry (loose)** | `\|rim − left_lip\| < 0.25 · max(left_lip − cup_low, rim − cup_low)` — "max" = the *larger* depth, the lenient reading | `rim_symmetry` "max" |
| 8 | **Handle walk** | from `ri+1`, up to 60 bars (`handle_max_bars` 60 — config overrides the code default of 50). Track `handle_low` = running min low | `handle_max_bars` 60 |
| 9 | **ROLLING RIM** (user spec 2026-07-20) | any pre-entry handle bar with `h > rim + 1e-9` **dethrones** the rim → re-search for the next peak-confirmed rim from that bar on, re-passing gates 2–7. A **tie does not dethrone** (strict `>`) | `rolling_rim` True |
| 10 | **Handle depth** | `rim − handle_low > 0.20 · (rim − cup_low)` → setup **dead**. Note the depth here is measured from the **right** rim; gate 4's band used the **left** rim | `handle_max_depth_frac` 0.20 |
| 11 | **4-bar floor** (user spec 2026-07-23) | entry bar `k ≥ ri + 3` (rim = handle bar 1 → earliest entry = handle bar 4). No momentum fast-lane under the rolling rim — an early break above the rim goes to gate 9, never to an entry. The legacy waiver survives only in fixed-rim mode for pile reproducibility | `handle_min_bars` 4 |
| 12 | **Entry** | first bar with `h[k] ≥ rim + $0.01` (and ≥1 completed handle bar, and gate 11) → **buy-stop fill at rim + $0.01** | `entry_offset_dollars` 0.01 |
| 13 | **Session cutoff** | entry bar's own timestamp must be ≤ **15:49 ET**, else the signal is dropped | (hard-coded `EXIT_CUTOFF`) |

Dedup inside one (symbol, day, timeframe): one event per entry bar (`taken_entries`); when
several left rims produce the same right rim, the earliest `li` wins.

**Exits** (in live priority order): stop-loss at `handle_low` (stop-market) · take-profit at
`entry + 6R` (limit) · **EOD flatten at the first bar stamped ≥ 15:49** (market). Intrabar
convention everywhere: **if one bar touches both stop and target, the stop wins** (pessimistic).

**Dead parameter:** `handle_ratchet_bars: 4` is read into `self.ratchet` and never used — a
fossil of the pre-2026-06-11 handle definition. Documented, kept for config compatibility.

---

## 3. Every parameter, one table

### Pattern (cup_coffee_config_v2.py → PatternDetector)

| Parameter | Value | Meaning |
|---|---|---|
| `cup_min_bars` / `cup_max_bars` | 15 / 60 | cup length in bars, rim to rim |
| `right_rim_recovery_frac` | 0.25 | reused twice: the rim band (gate 4) AND the symmetry tolerance (gate 7) |
| `rim_symmetry` | "max" | loose symmetry (live); "min" = strict variant, compare-only |
| `rolling_rim` | True | THE rim definition (user decision 2026-07-20) |
| `handle_min_bars` | 4 | entry forbidden before handle bar 4 |
| `handle_max_bars` | 60 | give up if no breakout within 60 handle bars |
| `handle_max_depth_frac` | 0.20 | handle ≤ 20% of cup (the "5:1" rule) |
| `entry_offset_dollars` | 0.01 | buy-stop = rim + $0.01 |
| `handle_ratchet_bars` | 4 | **DEAD** — never referenced after init |
| `labeling.max_hold_bars` | 240 | hold cap in **bars**, not minutes (see §6.5) |

### Live bot (live_trader_ibkr.py CLI — defaults)

| Flag | Default | Notes |
|---|---|---|
| `symbols` (positional) | — | overrides everything |
| `--watchlist` | `auto` | newest `~/Downloads/*DayTrade*.txt`, else `data/watchlist.txt`, else built-in `["SPY","QQQ"]`. Parses raw TradingView exports (`###SECTION` headers, `NASDAQ:` prefixes). Warns if the file is >20h old |
| `--port` / `--host` / `--client-id` | 4002 / 127.0.0.1 / 8 | **refuses live ports 4001/7496** (a hidden `--i-understand-live` flag exists that bypasses the refusal — never use it) |
| `--arm` | off (shadow) | shadow logs "WOULD" lines, places nothing |
| `--risk` | 0.01 | risk per trade as fraction of equity |
| `--base` | 0 | virtual sizing equity (e.g. 10000); compounds with the session's account-wide P&L delta |
| `--tp` | 6.0 | take-profit in R (frozen evaluation winner) |
| `--minstop` | 0.25 | min stop as % of price; 0 = off (USER-APPROVED 2026-07-16) |
| `--max-positions` | 5 | own-book count: pendings + shadow opens + real opens |
| `--tfs` | 1min,2min,5min | must include 1min (base stream); non-backtested tfs warn NO EVIDENCE |
| `--delayed` / `--poll` / `--replay` | off | delayed tier / polling fallback (interval `max(30, 12·n_syms)`s) / evening rehearsal. `--replay --arm` refused |

Derived sizing: `qty = int(0.01 × equity ÷ (trigger − stop))`; skip if qty < 1.
While the handle deepens, the stop only **ratchets down** (moves ≥ 0.5¢ lower), qty and
target are recomputed (`max(1, …)` on resize — a resize can keep a 1-share position that
the initial arm would have skipped).

### Cost model (evaluation harness — FROZEN)

| Item | Value | Where |
|---|---|---|
| Entry slippage | 2¢/share | all `bt_*` evaluations |
| Commission | 0.3¢/share per side | " |
| Round trip | `FEE = $0.026/share` | `net_R = raw_R − FEE / real_risk_per_share` |
| Exit slippage | **0 (assumed)** | TP is a limit, SL is stop-market — all slip is loaded on the entry |
| **Stale config keys** | `labeling.slippage_bps: 2`, `commission_bps: 0.5` | **NOT what the harness uses** — the cents-based model above is |

### Honest screens (applied at evaluation, NOT inside the detector)

| Screen | Rule |
|---|---|
| Real price | ≥ $15 **un-split-adjusted** (recovered via cached split factors; factor defaults to 1.0 when unknown) |
| Min stop | (entry − stop)/entry ≥ 0.25% (ratio identical on adjusted or real prices — the factor cancels) |
| Index exclusion | symbol must not contain "SPY" or "QQQ" (substring test) |

The events piles **contain** sub-floor and index events; the screens filter at analysis time.
The live bot enforces min-stop at arm time; it never enforces the $15 floor (the watchlist is
assumed to already be tradeable names).

---

## 4. How is the universe chosen? (three different answers)

This is the biggest gap between what the config *describes* and what *actually happens*.

1. **What the config describes** (`universe`: size-tiered catalyst screen — gap thresholds by
   market cap, earnings + revenue-growth rules, energy-beta exclusion): **designed, and DEAD.**
   It ran during the original 2021-2026 backfill (momentum-gap arm only — the FMP earnings
   endpoint was already dead then), and cannot run now because the Polygon subscription lapsed.

2. **What the backtest actually uses**: the **frozen universe** of (symbol, day, timeframe)
   combos the old backfill downloaded into `cache/minute/` — 1,266 non-index symbols,
   2021-06-28 → 2026-06-15. Re-detections under new rules (e.g. `events_cup_current.jsonl`)
   re-scan exactly those cached combos. No new gapper days can be discovered until a data
   source returns (open decision: Polygon $79/mo vs FirstRate $599.95 one-time).

3. **What live actually uses**: a **hand-picked TradingView watchlist**, exported each morning
   to `~/Downloads/*DayTrade*.txt`. Non-stock rows (TVC:VIX, TVC:USOIL) are auto-skipped via
   the conId check. **No code screens the live list** — price floor, liquidity, catalyst are
   all the human's judgment.

**Implication (§6.9):** live population ≠ backtest population. The +0.226R/trade backtest
evidence belongs to the systematic gapper screen; a hand-picked list of calm large-caps is a
different (worse) population, and the min-stop screen is what mostly protects live from it.

---

## 5. When is the signal computed, and what price does it trade?

**Signal timing — everything runs on CLOSED bars:**
- The 1-min stream feeds `feed_1min` (accepts 09:30–16:00 inclusive); 2/5-min bars are built
  locally by `MinuteAggregator` in **clock-aligned buckets anchored at 09:30** and emitted the
  moment the bucket closes.
- After each closed bar of a timeframe, `reconcile()` re-scans that series. When a valid cup's
  handle reaches the state "the NEXT bar may legally enter" (`len(bars) ≥ earliest`, i.e. **at
  the close of handle bar 3**), the bracket is placed. The resting order therefore exists
  before handle bar 4 opens — a first-touch fill on bar 4 is possible, exactly as backtested.
- A dethrone (bar high > rim) cancels the pending bracket on the closed bar that did it.
- Bars stamped ≥ 15:49 never reach `reconcile`; the first such bar triggers the global EOD
  flatten (once), and shadow stop/TP exits on that same 15:49 bar are honored **before** the
  flatten. ✅ **A verified hole (found 2026-08-24, FIXED 2026-09-02):** the 15:49 **1-min** bar, after
  firing the flatten, still feeds the aggregators — which flush the 15:48 2-min and 15:45
  5-min buckets. Those bars are stamped *before* 15:49, so they DO reach `reconcile`, whose
  pending slot the flatten just cleared — and can arm a **new** bracket *after* the EOD
  flatten. In `--arm` mode nothing cancels it (the post-EOD cleanup branch is shadow-only):
  a DAY buy-stop rests until 16:00 and, if it fills, the position can survive overnight.
  **The fix:** live mode never arms again once the day's flatten has fired
  (`eod_done and not replay` guard in `reconcile`); replay is exempt on purpose — its
  sequential symbol walks are why the naive gate broke replay in July. Both directions
  are locked in by tests (`test_no_arming_after_eod_flatten`,
  `test_replay_still_arms_after_first_symbols_eod`).

**Prices traded:**

| Leg | Order type | Assumed fill (backtest/shadow) | Real fill (armed) |
|---|---|---|---|
| Entry | BUY STP at rim+$0.01, DAY | exactly rim+$0.01 (pile); shadow ledger uses `max(trigger, bar open)` — gap-over aware | actual, logged with slip in `data/paper_fills.csv` |
| Take-profit | SELL LMT at entry+6R (OCA) | exactly entry+6R | actual |
| Stop-loss | SELL STP at handle low (OCA) | exactly the stop price | actual (can slip through) |
| EOD | market flatten 15:49 | last known close | actual market |

Orders are a native IBKR bracket (parent + OCA children, `orderRef = cuph-SYM-tf-DATE-rimN`),
so the exits live at IBKR even if the bot dies. Cancelling the parent kills the children.

**Live prices are raw IBKR TRADES prices; research prices are split/dividend-adjusted Polygon
prices** (with real price recovered separately for the screens). Same-day this is identical;
it matters only for cross-day comparisons.

---

## 6. Implicit assumptions — the honest list

Things the code assumes without saying so. Each one is a place reality can diverge.

1. **Entry fills at exactly rim+$0.01 in the pile.** If the breakout bar *gapped over* the
   trigger, a real stop order fills at the worse gap price. The backtest's flat 2¢ slippage
   is the proxy for this; the live ledger's JBHT −4.79R (91¢ gap over a 24¢ stop) is the
   real-world case that motivated the min-stop floor. (The shadow/replay ledger *does* model
   it: entry = `max(trigger, bar open)`.)
2. **The stop fills at exactly the stop price.** No gap-through modeling; a real stop-market
   can fill lower. Loss labels are exactly −1.00R.
3. **Intrabar path is unknown.** When one post-entry bar spans both stop and target, the code
   books the stop (pessimistic). But the **entry bar itself is never exit-checked** — every
   labeler (backtest, detector, shadow) starts walking at the bar AFTER entry. So when the
   entry bar spans both trigger and stop, the backtest books the entry and ignores that bar's
   collapse; the loss registers only if a *later* bar reaches the stop. On the entry bar the
   convention is **optimistic**, not conservative. (Armed mode is immune: the real OCA at IBKR
   handles same-bar stop-outs correctly.)
4. **MFE (`peak_R`) is measured on bar highs** — it is "what a resting limit at that exact
   price could have caught," not "what you could have discretionarily sold."
5. **`max_hold_bars = 240` counts BARS, not minutes.** 240 bars = 4 hours on 1-min but
   20 hours on 5-min — i.e. on 2/5-min the 15:49 cutoff always binds first and the hold cap
   is effectively dead. Different timeframes trade different effective time-stops.
6. **Live and backtest build 2/5-min bars differently.** Live: clock-aligned buckets anchored
   at 09:30. Backtest: `data_layer.downsample()` chunks the RTH 1-min list **by array index**,
   so any missing minute phase-shifts every later bucket. (The live bot's docstring blames
   "Polygon's own 2/5-min files" — wrong: the pipeline only ever fetches 1-min and aggregates
   locally; the re-anchoring is our own downsampler's artifact.) Verified ~88% of signals
   reproduce exactly on thin names, 100% on full-coverage days; the rest are the same setup
   phase-shifted. Accepted; clock alignment is the correct live choice.
7. **Vendor bars differ.** IBKR vs TradingView vs Cboe-One feeds disagree ~1.3% of bars by
   ≥1¢ at the highs; ties at rims can flip a peak decision. Measured robustness: 98.9% of
   signals survive realistic 1¢ jitter. `_is_peak` uses `≥` (a tie IS a peak); dethrone uses
   strict `>` (a tie does NOT dethrone).
8. **Cross-timeframe duplicates: live and backtest count differently.** Live enforces ONE
   pending per symbol across all timeframes; ad-hoc research piles (e.g.
   `events_cup_current.jsonl`) re-detect each timeframe independently and can count the same
   breakout 2-3×. The official backfill deduped cross-tf; re-detections don't. Per-trade
   stats survive; total-R sums overstate what one live account would have taken.
9. **The live watchlist is not the backtested universe** (§4). The edge evidence is for
   systematically-screened gappers; hand-picked lists are a different population.
10. **Sizing equity is account-wide.** `--base` virtual equity moves with the whole account's
    NetLiq delta — including P&L from the tight-flag robot sharing the account. Slot/position
    accounting (`occupied_syms`) is own-book, but equity is not.
11. **The Sharpe printed by evaluations is over trading days only** (days with ≥1 trade),
    annualized ×√252. Flat days don't dilute it; it is not a portfolio NAV Sharpe.
12. **Timestamps are naive ET.** Tick-mode (delayed tier) stamps bars wall-clock − 15 min —
    approximately the bar's market time, good enough for bar sequencing, not for research.
13. **Data validation differs by path.** The backfill path used `DataLayer._validate`
    (≥60 bars, ≤3 zero-volume bars, no bad ticks). Ad-hoc `bt_*` scripts use looser ad-hoc
    length checks. The live bot trusts IBKR bars with **no validation at all**.
14. **There is no lunch rule anywhere in the code** — entries fire all day, exits are
    15:49-capped. The comments in `research_data.py` explicitly document the rule's absence;
    the "lunch shortcut" that exists in the dashboards is a data-file toggle in `app.py`
    (pre-built event piles), not detector logic.
15. **`baseline.win_rate: 0.60` in the config is aspirational fiction.** Measured win rate of
    the live rules at 6R TP is ~25% (the strategy's economics are few-big-winners, not
    most-trades-win). Nothing in the code gates on this value.
16. **The detector's `target_price` (measured move) and `outcome/pnl_R` labels are vestigial**
    in signals-only mode — all real evaluation uses `label_full_path`'s fixed-TP grid.
    Config keys `labeling.entry: "breakout_close"`, `"target": "measured_move"`,
    `hold_checkpoints_min`, `history_start/end: 2024` are stale descriptions of a v1 design.

---

## 7. The evaluation harness (FROZEN — do not re-slice)

- **Labels:** `label_full_path` walks entry→stop/time-cap with NO profit target, recording
  exact realized R at every fixed TP 1R..20R plus full MFE/MAE. One replay answers all TPs.
- **The frozen choices:** full-path labels · TP = 6R · min-stop screen ON at 0.25% · index
  excluded · net of $0.026/share ÷ real risk. Changing any of these ad hoc invalidates
  comparability with every recorded result (see memory: keep-evaluation-harness-fixed).
- **Null benchmark method** (the reusable core): random entries in the SAME sessions with the
  SAME dollar stops, labeled by the same `label_full_path`, same cost model → Welch t on the
  difference. This is what closed the index question.

**Current verdicts (as of 2026-08-24):**

| Population | Result | Status |
|---|---|---|
| SPY/QQQ 5-min, 22 yr (n=1080) | −0.293 R/tr, t=−6.00 vs random | **CLOSED: no edge** |
| SPY/QQQ all six feasible tfs, 2.5 yr (n=1502) | −0.056 R/tr, t=−2.08 vs random | **CLOSED: worse than random** |
| Gappers, today's rules (n≈1379 honest) | +0.226 R/tr, t=+3.29, all years positive | sharpened null (2026-09-02, paired, 10 draws/trade): vs clean whole-session random **t=+1.96**; vs fair time-matched (after-entry-only) random **t=+3.89**. ⚠️ A ±30-min matched window is CONTAMINATED — draws before the entry embed the future breakout (they "earn" +1.47R/tr, unachievable). Fill-fantasy bound (same audit): 5.9% of entries gap over the trigger (p95 = 1.00R worse); 11.5% of entry bars touch the stop that no labeler checks — worst-case −146R of the +312R edge |

Contamination note: every rule choice (rolling rim, loose symmetry, 0.25% floor, $15, 6R TP)
was made after seeing the full 2021-2026 gapper pile → those numbers are in-sample. The only
clean out-of-sample instrument for the frozen rules is the **forward paper ledger**
(`data/replay_trades.csv`). The SPY/QQQ splits (develop/validate/lockbox) are clean for
*future* rule work only.

---

## 8. Data files inventory

| File | What it is | Trust |
|---|---|---|
| `data/events_fixedrim.jsonl` (10,775) | gappers under OLD fixed-rim rules, 2021-2026 | reference only |
| `data/events_cup_current.jsonl` (5,046) | same cached universe re-detected under TODAY's rules | current gapper evidence (cross-tf dupes, §6.8) |
| `data/events_cup_ibkr5.jsonl` (1,080) | SPY/QQQ 5-min 22 yr under today's rules | index verdict |
| `data/events.jsonl` (2,524) | **CORRUPT** rolling-rim backfill — 100% SPY/QQQ (Polygon 429s silently dropped every gapper) | **do not use** |
| `data/replay_trades.csv` / `.html` | daily two-variant forward ledger (minstop 0.25 vs 0), `peak_R` = MFE | the real out-of-sample record |
| `data/paper_fills.csv` | armed-mode fills with slippage vs intended level — **does not exist yet**: no armed order has ever filled (created on first fill) | fill-quality measurement, still empty |
| `data/splits/` | SPY/QQQ develop/validate/lockbox + ACCESS_LOG | Phase-0 discipline |
| `cache/minute/`, `cache/ibkr5/`, `cache/ibkr1min/` | bar caches (Polygon frozen; IBKR refreshable) | offline research fuel |
| `data/mined_table_factors.json`, `factor_scores.json` | factor-mining artifacts (OLD pile — stale until re-mined) | re-mine before use |

---

## 9. Decisions baked into the code (who decided what, when)

| Date | Decision | Where it lives |
|---|---|---|
| 2026-07-16 | min-stop 0.25% floor (after revert + re-approval) | `--minstop` default, arm_pending |
| 2026-07-20 | **Rolling rim is THE rim definition** (chosen against the head-to-head: +353R vs +535R, known and accepted) | `rolling_rim: True`, EXCEED path |
| 2026-07-22 | daily two-variant replay ledger | replay branch, replay_record.py |
| 2026-07-23 | **universal 4-bar floor, no momentum fast-lane** | `momentum = (not rim_roll) and …` |
| 2026-07-27 | own-book accounting (two robots share the account) | `occupied_syms()` |
| ~2026-07 | loose ("max") rim symmetry live — won head-to-head | `rim_symmetry: "max"` |
| frozen | TP 6R, EOD 15:49, 2¢+0.3¢ cost model, full-path labels | eval scripts, `--tp` default |

---

## 10. File directory — every file, its home, its purpose

*Reorganized 2026-08-25: the live path and core engine stay at top level (imported by name —
moving them breaks the system); the offline research pipeline lives in `research/`; one-off
utilities in `tools/`; replaced code in `archive/superseded/` (kept, never deleted). Every
moved file carries a 4-line shim that finds the project root, so `python research/x.py` works
from anywhere. The five `*_tightflag*` files are a SEPARATE strategy's namespace (flat, by
that project's own convention) — do not mix them into cup-coffee work.*

### Top level — live path & core engine (do not move these)

| File | Purpose |
|---|---|
| `pattern_detector.py` | THE strategy, single copy: turns one day of bars into entry/stop/R events (rolling rim, signals_only live path) |
| `cup_coffee_config_v2.py` | the single CONFIG dict — pattern geometry, screens, labeling; §3 lists which keys are live vs stale |
| `data_layer.py` | `Bars`/`IntradaySeries` contracts, per-day quality validation, index-chunked `downsample()` |
| `research_data.py` | disk-cached Polygon/FMP access + `session_end_idx` + `label_full_path` (the 1R..20R full-path labeler) |
| `live_trader_ibkr.py` | the IBKR paper order robot: watchlist → stream → pre-armed brackets → EOD flatten; `--replay` writes the ledger |
| `explain_day.py` | "why didn't we trade X?" — every cup candidate and the exact gate that rejected it, with near-misses |
| `replay_record.py` | the one-command evening ritual: two-variant replay → `replay_trades.csv` + `.html` → explain_day |
| `splits.py` | Develop/Validate/Lockbox discipline for SPY/QQQ with code-enforced, logged access budgets |
| `app.py` | main Streamlit research dashboard (piles, enrichment panels, head-to-heads, trade charts) |
| `app_trades.py` | small companion viewer: cost sliders, MFE histogram, cumulative R, trade pictures — ⚠️ defaults to the OLD fixed-rim pile unless `PILE`/`TABLE`/`RP` env vars point elsewhere |
| `*_tightflag*.py` (5 files) | the separate tight-flag strategy (its own detector, bot, scanner, viz, history puller) — different chat channel, hands off |
| `tests/test_cup_coffee.py` | the safety net: 14 hand-built cases, one per gate (§2) + labeler conventions + live/backtest parity + a real-day golden test. Run `python tests/test_cup_coffee.py` after ANY strategy-code change |
| `README.md` / `PLAYBOOK.md` / `GOING_LIVE.md` | repo front door / quant-loop study guide / going-live staircase. This file (§10) is the authoritative directory |

### research/ — the offline pipeline (stages 1–5)

| File | Purpose |
|---|---|
| `universe.py` | Stage 1: size-tiered gap-catalyst watchlist builder (pure library + demo) |
| `providers_historical.py` | point-in-time BACKTEST universe from Polygon grouped-daily (delisted included — survivorship-correct) |
| `providers_real.py` | live-day providers: TradingView snapshot scan + Polygon 1-min bars |
| `backfill.py` | the engine: universe → detect → full-path label → append to the pile; resumable |
| `run_history_v2.py` | current launcher: full ~5-year pile rebuild on the v2 spec |
| `daily_update.py` | after-close routine: append today's events, re-score factors (also run by the GitHub Action) |
| `rebuild_labels.py` | fast re-label of the pile from cached bars (no factor recompute) |
| `enrich_real_price.py` | recover REAL un-split-adjusted price + reverse-split count → `data/realprice*.json` ($15 screen input) |
| `enrich_commodity.py` | SIC-based commodity-sector flag per ticker → `data/commodity.json` |
| `enrich_entry_type.py` | tag each trade momentum vs consolidation → `data/entry_type.json` |
| `feature_library.py` | the ~28 point-in-time factors (technical/macro/fundamental/news/calendar + noise canaries) |
| `factor_scorer.py` | Stage-4 scoring engine: IC, win-rate splits, significance |
| `mine_factors.py` | assemble the trade×factor table, score every factor × take-profit, Benjamini-Hochberg FDR |
| `loop.py` | Stage-5 skeleton: promote passing factors, retire decayed ones |
| `claude_research.py` | the two Claude-API touchpoints: propose factor hypotheses, interpret decay |

### tools/ — one-off utilities & diagnostics

| File | Purpose |
|---|---|
| `dry_run.py` | look-ahead proof: replays busiest cached days bar-by-bar, checks every signal fires identically |
| `paper_sim.py` | dollar-based execution+sizing simulator (position cap, leverage, concurrency) — the drawdown studies |
| `polygon_check.py` | probe: does the Polygon plan stream real-time? (answer was no — delayed only) |
| `alpaca_check.py` | Alpaca paper-account smoke test (vendor rejected: no delisted-ticker endpoint) |
| `viz_trades.py` / `viz_batch.py` | candlestick PNGs of detected trades (one / a diverse batch) → `charts/` |
| `build_nb.py` | generates the teaching notebook `data_cleaning_walkthrough.ipynb` |
| `study_data_layer.py` | learning aid: what each data-side file produces for one example trade |

### archive/superseded/ — replaced or applied (kept for reference, never run)

| File | Why it's here |
|---|---|
| `live_signals.py` / `live_signals_ibkr.py` | signal-only precursors of the order robot — superseded by `live_trader_ibkr.py` |
| `run_today.py` / `run_extend.py` | old launchers — superseded by `daily_update.py` / `run_history_v2.py` |
| `enrich_cup_strictness.py` | superseded by the strict re-run pile |
| `patch_strict_gap.py` | one-shot backfill patch, already applied (commit 94e4642) |
| `test_detector.py` | tested the pre-v2 ATR detector — imports a function that no longer exists. Replaced by `tests/test_cup_coffee.py` |
| `live_trader_ibkr.py.bak-*` | pre-2026-07-27 backup of the bot |

`archive/junk/` holds shell accidents (a captured traceback, a heredoc fragment) — deletable,
parked per the back-up-before-deleting rule. `archive/old_runs/` predates this cleanup.

---

## 11. How to verify any claim in this document

```bash
python explain_day.py SYM                # why a symbol did/didn't trade today, gate by gate
python live_trader_ibkr.py --replay --delayed   # rehearse the last completed session
python replay_record.py                  # evening: ledger + html + explanations
python splits.py --status spy_qqq        # split budgets and access log
```

The detector's five-stage docstring (`pattern_detector.py` top) is the narrative version of
§2; `scan_setups`' docstring explains the live/backtest timing equivalence proof.
