# Going Live — a deep guide (paper → real money), for the Cup-and-Handle strategy

Companion to `PLAYBOOK.md`. This is the **riskiest phase of the whole project** — where a
backtest meets the real tape. Read it as: *here is each piece, what it does, and ⚠️ where it bites.*

Markers: 💡 plain English · 🎯 our strategy specifically · ⚠️ **pay attention here**.

> **Prime directive:** your edge is **thin (+0.22R/trade net)**. Assume live will be *worse* than
> backtest (slippage, fills, lag). Paper trading exists to **measure how much worse** before any
> real money. Most good-looking backtests underperform live — the question is by how much, and
> whether +0.22R survives it.

---

## 1. The staircase — and the GO/NO-GO at each rung
Never skip a rung. Each one *kills* strategies that passed the one before.

| rung | what it proves | GO criterion |
|---|---|---|
| **Backtest** ✓ | the rules have an edge on history (net, real-price) | done |
| **Walk-forward** | the edge isn't an artifact of choices made on the full data | held-out (e.g. 2025–26) edge ≈ in-sample edge |
| **Paper** (live data, fake $) | the live *logic + signal timing* work; first read on slippage | live signals match the backtest; fills within tolerance |
| **Small live** (real $, tiny) | the *real fill quality* (paper fills lie) | real fills ≈ paper fills; net still positive |
| **Scale** | nothing broke at size | drawdown + behavior as expected |

⚠️ **The #1 mistake is skipping paper** (or going live on backtest hope). The #2 mistake is trusting
*paper* fills as if they were real (see §2).

---

## 2. Broker choice (deep)
| | **Alpaca** | **Interactive Brokers (IBKR)** |
|---|---|---|
| API | simple REST + websocket | TWS/Gateway + `ib_insync` (heavier) |
| paper | free, instant to set up | yes, more realistic |
| cost | commission-free (PFOF → hidden slippage) | low explicit fees, smart routing |
| best for | **first connection + the dry run + logic test** | **real fill realism + real money** |

⚠️ **Alpaca paper fills are *optimistic*** — they fill instantly at the trigger with little/no
slippage. So **paper P&L flatters you.** Use Alpaca paper to validate *logic and signal timing*,
**not** fill quality. The honest fill test is **small real money on IBKR.** ❌ Not CTP/SimNow (China
futures — can't trade US stocks).

---

## 3. The five live modules — deep, each with its watch-list

### A. Live market data
💡 Real-time 1/2/5-min bars for the day's gappers, *as they form* (Polygon websocket or broker feed).
- ⚠️ **The forming-bar trap:** a minute bar isn't final until the minute closes. **Detect only on
  *closed* bars** — using the half-formed current bar's high/low is look-ahead = a fake edge.
- ⚠️ **Peak confirmation lag:** the detector's `_is_peak` needs the *next* bar to confirm a peak. Live,
  you can only confirm one bar *later* than the backtest assumed. So **live detection lags the
  backtest by ~1 bar** — bake that in; don't expect identical entry bars.
- ⚠️ **Live data is dirty:** bad ticks, halts, gaps. The backtest's `_validate` dropped these; live you
  must detect and skip them too, in real time.
- ⚠️ **Latency** (data delay + your compute + order round-trip): minor here because the entry is a
  *resting* buy-stop already at the exchange — but it matters for *detection→place*. Seconds are fine on minute bars.

### B. Strategy engine (real-time)
💡 Run `pattern_detector` on each symbol's live bar stream; when a valid cup+handle completes, place
the buy-stop. Same brain, now running forward in real time instead of replaying.
- ⚠️ **You're now stateful.** The backtest was stateless (replay). Live you track open positions,
  pending orders, which symbols are "in a setup." **Most live bugs hide in state.**
- ⚠️ **Idempotency:** if your loop re-runs (reconnect, restart), it must NOT place the same order twice.
  Tag every intended order with a unique client-id and check before sending.
- ⚠️ **One source of truth:** the *broker* is the truth for positions/orders, not your in-memory state.
  Reconcile against it (see D).

### C. Execution / order management (OMS)
🎯 Entry = a **buy-stop** at rim+$0.01; exit = a **stop-market** at the handle low; plus **flat by 15:49**.
The good news: these are broker-native order types — place them and the exchange does the work.
- ⚠️ **Entry gap-through:** a buy-stop fills at the *next available* price ≥ trigger. On a fast gapper it
  can fill **well above** rim+$0.01. **Log realized entry vs trigger** — this is your real slippage,
  vs the 2¢ you assumed. On the thin edge, this is make-or-break.
- ⚠️ **Stop-market slippage:** stops fill at market on trigger → on a fast drop your −1R can be −1.3R.
- ⚠️ **Bracket = three linked orders, OCO exit:** the entry buy-stop, *on fill*, places BOTH a
  stop-loss (stop-market at the handle low) AND a take-profit (limit at entry + k·R) as a
  **One-Cancels-Other pair.** **When either exit leg fills, the position is flat → the other leg is
  auto-cancelled** (TP hits → cancel the stop; stopped out → cancel the TP). Never a filled position
  without a live exit, never a live exit without a position. (EOD flatten, below, cancels both + closes
  if neither has hit by 15:49.)
- ⚠️ **Alpaca specifics (verified):** `order_class=bracket` **cannot use a stop entry** — bracket/OTO
  entries are *market or limit only*. So our buy-stop entry must be a **standalone `stop` order**, and
  then **on its fill event** (TradingStream trade-updates) we place the exit as a **sell
  `order_class=oco`** (take-profit limit + stop-loss stop). That *is* the two-step above, made concrete.
  EOD/kill = `cancel_orders()` + `close_all_positions(cancel_orders=True)`.
- ✅ **FINAL DESIGN (built, IBKR): the PRE-ARMED native bracket** — `live_trader_ibkr.py` places the
  buy-stop bracket **while the handle is still forming** (a rim is only confirmed one bar after it
  prints, and the earliest legal entry is ≥1 bar after that — so there is always time to arm). The
  resting order then fills at the **first touch** of rim+$0.01, matching the backtest's assumption;
  arming *after* the breakout bar closes would instead fill at wherever price ran to (~0.5R worse on
  fast momentum bars). Each closed bar the stop/qty/target are modified as the handle low drifts;
  the pending bracket is cancelled if the handle invalidates. Verified backtest-faithful on 183/183
  real entries (armed in time, exact trigger+stop, no early fills). Fills log to
  `data/paper_fills.csv` with slippage vs the intended level — the live-vs-backtest measurement.
- ⚠️ **Partial fills:** size the stop to the *filled* quantity, not the intended one.
- ⚠️ **Rejections:** handle insufficient-buying-power, halted-symbol, etc. — don't crash or silently miss.
- ⚠️ **End of day:** cancel unfilled buy-stops *and* flatten open positions by 15:49. Never leave a
  resting entry overnight (a gap could fill it Monday).

### D. Risk / monitoring — **pay the most attention here**
This layer protects your capital from *your own bugs*. It matters more than the strategy.
- **Position cap** — max N concurrent positions (a gap morning can fire many signals at once).
- **Risk per trade** — size so each trade risks a fixed % of equity (the R→% rule, PLAYBOOK §4.1).
- **Max daily loss** — auto-kill if down Y% on the day.
- ⚠️ **Kill-switch** — one button (manual *and* automatic) that **cancels all orders + flattens all
  positions.** The single most important thing you build.
- ⚠️ **Reconciliation** — continuously check *"does my internal state == the broker's actual
  positions/orders?"* Any drift = a bug → halt and alert. Drift is how small bugs become big losses.
- ⚠️ **Crash recovery** — if the script dies mid-position, on restart it must **read the broker's real
  positions/orders and reconcile BEFORE doing anything.** (Your resting stop at the broker still
  protects you even if your script is dead — *if* you placed it as a bracket. That's why D.bracket matters.)
- ⚠️ **Disconnection** — define behavior if the data/broker feed drops mid-session.

### E. Data store / logging
💡 Log every signal (with bar context), order (placed/filled/rejected + prices), fill, and position change.
- ⚠️ **This powers live-vs-backtest reconciliation:** for each live trade, compare the realized
  entry/exit/R to what the backtest *would* have recorded. **Persistent divergence = your real edge
  ≠ your backtested edge** — that's the signal to stop and investigate, not push more size.

---

## 4. Backtest-vs-live gaps — where reality bites
| gap | backtest assumed | live reality | how you measure it |
|---|---|---|---|
| entry fill | exactly rim+$0.01 + 2¢ | buy-stop gaps through | log realized entry − trigger |
| exit fill | exactly stop / target | stop-market slips on fast moves | log realized exit − stop |
| bar timing | acts on completed bars instantly | ~1-bar detection lag (peak confirm) | compare live signal bar vs backtest |
| data | clean cached bars | live ticks, halts, revisions | handle bad ticks/halts in real time |
| costs | 2¢ slip + 0.3¢ comm | real spread + slippage + fees | reconcile actual fills |
| capital | unlimited, no concurrency | finite; position cap; PDT | sizing + cap rules |

⚠️ **The thin edge is the whole story:** at +0.22R/trade, just a few extra cents of average slippage
can flip it negative. **Measuring real slippage in paper→small-live is the make-or-break test.**

---

## 5. Strategy-specific watch-outs (Cup-and-Handle on gappers)
- ⚠️ **Gappers are volatile & can be hard to fill** — wider spreads, faster moves, more slippage than a
  calm stock. Your real-price/$15 screen already removed the worst (penny) names; live will test
  whether the $15+ gappers fill cleanly. **Watch realized slippage by symbol.**
- ⚠️ **Halts / circuit-breakers (LULD):** gappers get halted intraday — your resting order can be stuck
  or fill weirdly on resume. Detect halts and handle them.
- ⚠️ **Signal clustering:** ~2 trades/day on average, but a big-gap morning can fire many at once →
  position cap + sizing so you don't over-leverage in one regime.
- ⚠️ **PDT rule:** US margin + intraday needs **$25k** (4+ day-trades/5 days). Below that = 3
  day-trades/week (kills it) or a cash account (T+1 settlement caps recycling). A *precondition*, not a feature.
- **The open:** the first minutes are the wildest (spreads/slippage), but your cup needs 15–60 bars to
  form first, so you rarely trade the very open — a small mercy.
- **The premarket screener** (`/tmp/screen.py`-style, mirroring `universe.py`) is your live universe
  feed — run it before the open to know which symbols to watch.

---

## 6. Operational risks — the "what actually blows accounts up"
Every one of these has wiped real accounts. They're not strategy risks; they're *engineering* risks.
- **Double-fills / duplicate orders** (no idempotency).
- **A filled entry with no protective stop** (not bracketed) — one gap and you're unhedged.
- **Runaway loop** placing orders in a tight cycle.
- **Stale data** → trading on old prices.
- **Connection loss** mid-trade with no recovery plan.
- **Time/calendar bugs** — DST, holidays, half-days, the 15:49 cutoff off-by-one.

⚠️ The risk/monitor layer (§D) + kill-switch + reconciliation are **non-negotiable** — build them
*before* you ever place a paper order, not after.

---

## 7. Position sizing & money management (live)
- **R→$ (your spec):** risk **R$ = 1% of the *current* portfolio** per trade ($10 on $1,000; compounds
  as the account grows). **Shares = R$ ÷ per-share-stop** (entry − stop). With the min-stop 0.25% screen
  the stop is ≈0.25%+ of price, so the **notional runs up to ~$4,000 on a $1k account ≈ ~4:1 leverage**,
  which intraday margin provides — so there's no "can't afford it" case. Dollar P&L = realized-R × R$.
  ⚠️ At **1%/R** the historical ~60R drawdown ≈ a **~60% account drawdown** — steep. `paper_sim.py`
  reports the real number so you can see it and decide; 0.25–0.5%/R is gentler if that's too much.
- **Max concurrent positions** so a signal cluster can't over-leverage you.
- **Daily / weekly loss limits** wired to the kill-switch.
- Start paper, then live, at the **smallest** size — you're buying *information* (real fills), not profit.

---

## 8. Pre-flight checklists
**Before paper:** broker paper keys · live data feed working · detector runs on live bars (dry-run,
prints signals, no orders) · kill-switch + reconciliation built · logging on.
**Before real money:** weeks of paper logged · live signals reconciled to backtest · slippage measured
and edge still positive after it · account funded ≥ $25k (PDT) · smallest size · daily loss limit set ·
a manual kill-switch you can hit in one second.

---

## 9. Where to pay the MOST attention (prioritized)
1. **Live fill quality vs the +0.22R edge** — slippage is the make-or-break. Measure it relentlessly.
2. **The safety layer** (kill-switch, reconciliation, crash recovery) — protects capital from bugs.
3. **No look-ahead in the live loop** — act on *closed* bars only; accept the ~1-bar lag.
4. **Order safety** — always bracket entry+stop; idempotency; EOD flatten.
5. **PDT / account size** — a precondition before any of this matters.

**Order of operations:** walk-forward → dry-run (signals only, no orders) → build the safety layer →
paper with orders → reconcile live-vs-backtest → small real money. The strategy is the easy part now;
the *plumbing and the safety* are where the work and the danger are.
