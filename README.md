# Cup Coffee ☕ — intraday cup-and-handle research

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
| `pattern_detector.py` | the v2 detector — cup → handle (ratchet + buy-stop entry) → stop/target/session-exit label |
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
| `research_data.py` | disk-cached Polygon access · `label_full_path` (exact realized R at every take-profit) · session-exit logic |
| `feature_library.py` | ~28 point-in-time factors (technical / macro / fundamental / news / calendar / noise-control) |

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
**✅ Tests** — `test_detector.py`

---

## Data files (`data/`, all gitignored)

| file | what |
|---|---|
| `events.jsonl` | every detected trade — geometry, entry/stop, measured-move label |
| `mined_table.json` | per-trade full-path labels (realized R at **1R–20R**) + factor values |
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
python run_history_v2.py          # rebuild the ~4-year pile (resumable — safe to stop/restart)
python mine_factors.py --rebuild  # re-mine factors + take-profit grid on the current pile
python daily_update.py            # detect today's trades and re-score
python test_detector.py           # detector unit checks
```

---

## Key concepts

- **R** — risk per trade = entry − stop. All P/L is in R, so trades compare fairly.
- **Full-path label** — instead of one baked-in target, every trade records its result at *each* fixed take-profit (1R…20R) plus its peak (MFE). That's what lets us answer "take 1R or hold?"
- **Min stop** — a real pattern needs a stop that's a meaningful % of price; sub-noise tiny stops (handle ≈ 0) produce nonsense R-multiples and are filtered (slider in the dashboard).
- **Session rule** — no entries 11:00–13:00; morning entries flat by 11:00, afternoon entries flat by 15:50.
- **Noise canary** — fake random factors are scored alongside real ones; if they "pass," the bar is too low.
