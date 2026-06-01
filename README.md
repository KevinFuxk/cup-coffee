# Cup Coffee — Factor Research System (Reference)

A self-running engine that finds which factors make your intraday cup-and-handle
trades win, on US equities (1/2/5-minute charts). It scans for setups, labels
every outcome, scores factors against a 0.60 baseline win rate, keeps a track
record of each factor over time, and uses Claude to propose new ideas as old
ones decay.

---

## The files

| File | Stage | What it does |
|------|-------|--------------|
| `cup_coffee_config_v2.py` | — | All your settings in one place. Edit here, every stage reads it. |
| `universe.py` | 1 | Builds the daily watchlist: QQQ/SPY + stocks that cleared the gap catalyst. Size-tiered, tagged. |
| `data_layer.py` | 2 | Pulls and cleans the 1/2/5-min charts for each watchlist name. |
| `pattern_detector.py` | 3 | Finds cup-and-handles (lip rule, multi-handle) and labels each WIN/LOSS/TIME. |
| `test_detector.py` | — | Proof: 14 scenarios confirming the detector obeys every rule. Re-run after any change. |
| `factor_scorer.py` | 4 | Scores factors — finds real ones, rejects noise, splits the edge by size tier. |
| `loop.py` | 5 | The self-running loop: score → keep → retire decayed → ask for new ideas. |
| `claude_research.py` | 5 | The Claude touchpoint: proposes new factors, triages decayed ones. |
| `providers_real.py` | data | LIVE data wiring — TradingView (today's scan) + Polygon (bars) + FMP (earnings). |
| `providers_historical.py` | data | POINT-IN-TIME universe for the backtest — Polygon grouped-daily, delisted names included. |
| `backfill.py` | engine | Walks a range of days and fills `events.jsonl`. The run scripts below call it. |
| `run_today.py` | run | Runs the live pipeline for the most recent session. Use this first to prove your keys work. |
| `run_history.py` | run | Builds the full backtest history across a year (point-in-time, survivorship-correct). |

---

## How to run it

1. **Wire your keys (as environment variables, not in the code).** Get a Polygon
   key (bars + grouped-daily + energy beta), an FMP key (earnings), and an
   Anthropic key (the factor loop). `pip install requests tradingview-screener anthropic`.
   Put the three keys in `~/.zshrc`:
   ```
   export ANTHROPIC_API_KEY="..."
   export POLYGON_API_KEY="..."
   export FMP_API_KEY="..."
   ```
   then `source ~/.zshrc`. The run scripts read them with `os.environ`. Polygon
   needs the Starter plan (~$29/mo) — the free tier can't handle the call volume.

2. **Prove the wiring works.** Run `python run_today.py`. It runs the live pipeline
   for the most recent session and appends real events to `events.jsonl`. If it
   prints events, your keys and data are good.

3. **Build the history.** Run `python run_history.py` — test the 2-week range first
   (uncomment the line), then the full year. It's resumable (safe to stop/restart)
   and survivorship-correct (delisted names included). First pass captures pure
   momentum gappers; the earnings branch turns on once point-in-time revenue is wired.
   When done, `events.jsonl` holds your real training history.

4. **Turn on the four routines** (schedule in Claude Code):
   - **Universe** (each morning): build today's watchlist, save it.
   - **Data** (after universe): pull and clean today's charts for those names.
   - **Detect & label** (after close): find cup-and-handles, append to `events.jsonl`.
   - **Score & loop** (weekly): score all factors, update the track record,
     promote passers, retire decayed ones; if one is retired, ask Claude for
     replacements and show them to you.

5. **Review proposals.** When the loop proposes a new factor, you turn its
   description into a real feature function and add it to the library. This is
   the one manual gate — deliberate, so the machine never runs code it wrote
   about itself unchecked.

---

## What the terms mean

- **ATR** — how much a stock normally moves in one bar. Cup/handle depths are
  measured in ATRs so one rule fits every stock regardless of price.
- **WIN / LOSS / TIME** — a trade hits its target (win), its stop (loss), or runs
  out the clock (timeout). TIME = "nothing happened, exit flat."
- **R** — your risk: the distance from entry down to the stop. Profit is measured
  in R so all trades compare fairly ("made 3R").
- **IC** — how tightly a factor lines up with profit. Higher = stronger.
- **Lift** — a factor's top-half win rate minus the 0.60 baseline.
- **0.60 baseline** — the pattern's normal win rate. Factors must BEAT it to matter.
- **Conditioning by tier** — the same factor can be a huge edge on big companies
  and useless on small ones, so scores are computed per size tier, not just pooled.
- **Track record** — every factor's score is kept every cycle. It's how you tell
  a durable edge from a lucky one, and how decay gets caught.

---

## Honest caveats

- **Survivorship.** For a truthful backtest, the universe must include delisted
  names as they existed on each past day. Polygon does; TradingView's live
  snapshot does not — use TradingView for live trading, Polygon for history.
- **Real data.** Everything currently runs on synthetic charts. Wiring the real
  providers is what makes it produce real results.
- **Judge by consistency, not peak.** Don't crown the factor with the single best
  score — that's overfitting. Favor factors that are steady across time and regimes.
- **The detector's rules are proven; its eye is not yet.** The 14 tests prove it
  obeys your rules on clean shapes. Confirm it finds real, messy cups by eyeballing
  ~20 detected patterns on real data before trusting it fully.
