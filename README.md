# Cup Coffee ☕ — intraday cup-and-handle research

> **Layout note (2026-08):** the offline pipeline now lives in `research/`, one-off utilities in `tools/`, superseded code in `archive/superseded/`. The authoritative file-by-file directory is **what_the_code_actually_does.md §10**.

Detect intraday cup-and-handle setups on US-equity **gappers** (1/2/5-min charts),
label each trade's **full outcome path**, and mine **which factors tell you when to
take profit** (grab 1R, or hold for more). Survivorship-correct, point-in-time.

---

## Directory layout

```
.
├── *.py            the code — flat on purpose (the modules import each other)
├── data/           LIVE working state (gitignored, regenerable)
├── archive/
│   ├── data/         old trade piles & factor backups (reference only)
│   └── old_runs/     superseded run launchers (historical)
├── logs/           run logs (gitignored)
├── charts/         generated trade PNGs
├── cache/          Polygon API disk cache (~1 GB, gitignored)
└── .github/workflows/daily.yml    the after-close cloud routine
```

> **Why is the code flat?** Every module imports its peers directly
> (`from research_data import …`). Splitting them into subfolders would break those
> imports, so the code lives in the root and is grouped *logically* below instead.

---

## The code, by role

**🎯 Strategy core** — the pattern and its rules
| file | what |
|---|---|
| `cup_coffee_config_v2.py` | every tunable: cup/handle geometry, session windows, universe tiers |
| `pattern_detector.py` | the v2 detector — cup (±25% rim band) → handle (buy-stop at rim+$0.01; momentum vs consolidation split) → stop/target/session-exit label |
| `data_layer.py` | `Bars` container + 1→2→5-min downsampling |

**🌐 Universe** — what's eligible each day
| file | what |
|---|---|
| `universe.py` | size-tiered gap rules + the earnings-gapper rule |
| `providers_historical.py` | scans Polygon grouped-daily (whole market, incl. delisted) → gappers |
| `providers_real.py` | live snapshot provider for "today" |

**📊 Data & features**
| file | what |
|---|---|
| `research_data.py` | disk-cached Polygon access · `label_full_path` (exact realized R at every take-profit) · session-exit logic · corporate-`splits()` fetch |
| `feature_library.py` | ~28 point-in-time factors (technical / macro / fundamental / news / calendar / noise-control) |
| `enrich_real_price.py` | recover each trade's **real (un-split-adjusted) price** + reverse-split count from split history → `data/realprice.json` (drives the $15 screen + honest costs) |
| `enrich_commodity.py` | flag each ticker as **commodity-sector** (SIC-based) → `data/commodity.json` (drives the include-vs-exclude compare panel) |

**⛏️ Mining & scoring**
| file | what |
|---|---|
| `factor_scorer.py` | IC, win-rate split, significance test |
| `mine_factors.py` | assemble the table + score every factor × take-profit, Benjamini-Hochberg FDR, noise canary |

**🔁 Backfill & runs**
| file | what |
|---|---|
| `backfill.py` | the engine: universe → detect → full-path label → append; resumable via checkpoint |
| `run_history_v2.py` | rebuild the whole ~4-year pile (current launcher) |
| `run_today.py` | run just the latest session |
| `daily_update.py` | after-close routine (detect today → append → re-score); driven by the GitHub Action |

**🧪 Factor loop (stage 5, exploratory)** — `loop.py`, `claude_research.py`
**📈 Dashboard & viz** — `app.py` (Streamlit Trade Explorer), `viz_trades.py` / `viz_batch.py` (static PNGs)
**✅ Tests** — `tests/test_cup_coffee.py` (14 cases against the CURRENT rolling-rim rules; run after any strategy change)

---

## Data files (`data/`, all gitignored)

| file | what |
|---|---|
| `events.jsonl` | every detected trade — geometry, entry/stop, measured-move label |
| `mined_table.json` | per-trade full-path labels (realized R at **1R–20R**) + factor values |
| `realprice.json` | per-trade **real (un-split-adjusted) price**, real dollar-risk, and reverse-split count — built by `enrich_real_price.py` |
| `commodity.json` | per-ticker **commodity-sector flag** (SIC-based, true/false) — built by `enrich_commodity.py` |
| `factor_scores.json` | the factor × take-profit scoring output |
| `backfill.checkpoint` | last completed day (resume marker) |
| `backtest_v2.csv` | flat export of the pile |

Everything in `data/` is **regenerable from code + the Polygon API**, so it's gitignored.

---

## Local vs remote

- **Remote** — `github.com/KevinFuxk/cup-coffee`, branch `strategy-v2-rebuild`. **Code only** (data is gitignored).
- **Local** — code **+** `data/` (the live pile) **+** `cache/` (~1 GB of bars that make re-runs fast).
- **Daily Action** — `.github/workflows/daily.yml` runs `daily_update.py` weekdays at 22:00 UTC. ⚠️ Because data is gitignored, the cloud run recomputes the day but does **not** persist the pile — the durable pile is your **local** `data/`.

---

## Common commands

```bash
streamlit run app.py              # explore every trade + take-profit equity curves (localhost:8501)
python research/run_history_v2.py          # rebuild the ~5-year pile (resumable — safe to stop/restart)
python research/enrich_real_price.py       # tag trades with REAL (un-split-adjusted) price + reverse-split count
python research/enrich_commodity.py        # flag commodity-sector tickers (SIC-based) for the include/exclude compare
python research/mine_factors.py --rebuild  # re-mine factors + take-profit grid on the current pile
python research/daily_update.py            # detect today's trades and re-score
```

---

## Key concepts

- **R** — risk per trade = entry − stop. All P/L is in R, so trades compare fairly.
- **Full-path label** — instead of one baked-in target, every trade records its result at *each* fixed take-profit (1R…20R) plus its peak (MFE). That's what lets us answer "take 1R or hold?"
- **Min stop** — a real pattern needs a stop that's a meaningful % of price; sub-noise tiny stops (handle ≈ 0) produce nonsense R-multiples and are filtered (slider in the dashboard).
- **Real price & the $15 screen** — Polygon prices are split-**adjusted**, so a stock that later did a big **reverse** split shows a hugely inflated history (DBGI: real ~$6 in 2021 → shown as $678k). Since commission/slippage are fixed ¢/share, that inflation makes their cost look ≈ 0 when in reality (penny prices, cents-wide stops) it's brutal. `enrich_real_price.py` recovers the real price from split history; the dashboard then **costs every trade at its real price** and **drops sub-$15 (real-price) penny stocks by default**. `rev_splits` per trade = how many reverse splits that ticker ever did (a distress flag, shown not filtered).
- **Commodity exclusion (compare, don't dump)** — to avoid commodity names (oil/gas, gold/silver/copper/steel, coal, ag) and direct producers like Exxon, `enrich_commodity.py` flags each ticker by its **SIC industry code** — precise, because it catches producers but *not* industrials like Caterpillar that merely sell to miners (keyword-on-description would over-exclude). The dashboard shows an **include-vs-exclude compare panel** (return + risk) so you judge from the data before applying the exclusion.
- **Session rule** — two datasets, switchable in the dashboard's **Dataset** toggle: **all-day** (live: entries any time 9:30–15:49, morning trades held *through* lunch) and the older **lunch rule** (no entries 11:00–13:00, morning flat at 11:00). Both flat by 15:49.
- **Noise canary** — fake random factors are scored alongside real ones; if they "pass," the bar is too low.
