# Quant Research Playbook — a study guide, built from Cup Coffee ☕

This teaches the **whole backtesting loop** using our cup-and-handle project as the running
example. Goal: after reading it you can **swap in any future strategy and keep the skeleton.**

**How to read it.** Watch for these markers:
- 💡 **Plain English** — a term defined simply, before we use it.
- 🎯 **In our project** — how the idea looks for cup-and-handle (the concrete kicker).
- 🔄 **Any strategy** — the part that generalizes (what you'd change next time).
- ⚠️ **What bit us** — a real mistake from this project, so you spot it early.

> **The 80/20 truth.** The strategy idea is the *easy* 20%. **Data quality + realistic costs +
> not fooling yourself** is the 80% that decided whether Cup Coffee had a real edge. This guide
> spends most of its time there on purpose.

---

## The words you'll meet (read this once, refer back anytime)

| word | 💡 plain English |
|---|---|
| **R** / R-multiple | your result measured in *units of what you risked*. Risk $0.36, make $0.72 → **+2R**. Lose the risk → **−1R**. Lets a $3 stock and a $300 stock compare fairly. |
| **entry / stop** | where you buy / where you bail to cap the loss. **R = entry − stop** (in dollars). |
| **take-profit (TP)** | the rule for where you cash out. "TP = 3R" = sell once up 3× your risk. |
| **MFE** (max favorable excursion) | the best profit a trade *ever showed*, in R, even if you didn't capture it. |
| **MAE** (max adverse excursion) | the worst it went against you before (maybe) working. |
| **gross vs net** | gross = before costs; **net = after commission + slippage**. Only net is real. |
| **commission** | broker's per-share fee. **slippage** = getting a slightly worse fill price than you wanted. |
| **drawdown** | how far below its peak your account has fallen — the pain you must survive. |
| **Sharpe / Calmar (卡玛)** | reward per unit of bumpiness / yearly return ÷ worst drawdown. |
| **look-ahead bias** | using info you couldn't have known yet — cheating by accident. |
| **point-in-time** | only using what was knowable *at that moment*. The cure for look-ahead. |
| **overfitting** | tuning so tightly to the past that it fails on the future. |
| **walk-forward / Monte Carlo** | test on data you didn't tune on / re-run with random shuffles to see if luck carried you. |

---

## Meet the trade we'll follow: **AVXL**

We'll trace this **real trade from our pile** through the entire loop:

> **AVXL — 2021-06-28, 5-minute chart.** A gapper. Cup-and-handle formed; entry **$29.79**,
> stop (handle low) **$29.43**, so **R = $0.36**. It then ran up to **4.8R** at its peak (MFE)
> before pulling back.

Keep AVXL in your head — every stage below will do something to it, and the capstone at the
end walks it through all seven stages at once.

---

## 0. The loop in one picture

```
   ① IDEA        "intraday cup-and-handle on gappers pays if you hold past 1R"
        │
   ② DATA        pull 1-min bars (Polygon) → cache → clean (splits!, gaps, halts)
        │
   ③ DETECT      bars + rules → a signal → entry + stop          (the strategy)
        │
   ④ BACKTEST    replay bars → did it hit TP or stop? → an R result per trade
        │
   ⑤ COST        subtract commission + slippage → the NET R       (the honesty step)
        │
   ⑥ ANALYZE     stack all trades → equity curve, CAGR, drawdown, Calmar
        │
   ⑦ OPTIMIZE    improve it WITHOUT overfitting → validate → paper trade → live
```

Each numbered box is a section below. **The skeleton never changes between strategies — only
box ③ (the rules) and the universe in box ② do.** That's the thing to internalize.

---

## 1. Python & the toolbox

### 1.1 The Python that actually matters
💡 **Plain English:** you mostly need ways to *store* data and *look it up fast*.

| tool | what it's for | 🎯 In our project |
|---|---|---|
| **dict** (hash map) | instant lookup by a key | join a trade to its labels by `"AVXL\|2021-06-28\|5min\|..."` |
| **@dataclass** | a tidy record with named fields | `Bars` (one day of candles), `CupHandleEvent` (one signal) |
| **list** | an ordered series | the open/high/low/close arrays |
| **set** | dedup & "is it in here?" | the day's distinct tickers |
| **pure function** | same input → same output (testable, cacheable) | `label_full_path`, `cost_in_R` |

🔄 **Any strategy:** this never changes. You always store bars in arrays, signals in records,
and join results by a key.

### 1.2 pandas — and the one trap
💡 **Plain English:** pandas is a spreadsheet-in-code. Great for *analyzing*, slow for *tight loops*.
- ✅ Use for: grouping (`df.groupby('year')['R'].mean()`), reporting, joins, resampling.
- ❌ Don't use for: the per-bar detection loop over millions of bars.
- ⚠️ **What bit us nowhere — because we knew this:** we store bars as plain lists in `Bars`, not
  pandas, so detection is fast. **Rule: pandas at the edges, plain arrays in the core.**

### 1.3 The library stack
| library | role |
|---|---|
| **numpy / pandas** | math arrays / tabular analysis |
| **Backtrader** | the backtest *engine* (bars → strategy → broker → trades) |
| **QuantStats** | the *report card* (Sharpe, drawdown, Calmar, HTML tearsheet) |
| **Tablib** | export a table to csv/xlsx/json/html in one line |
| **vnpy** *(later)* | the *live-trading* framework (China futures) |

---

## 2. The backtest engine — a factory with 5 stations

💡 **Plain English:** a backtest is just *"replay history one bar at a time and pretend to trade."*
Backtrader splits that into 5 cooperating stations:

| station | Backtrader name | job | 🎯 cup-and-handle |
|---|---|---|---|
| **Manager** | `Cerebro` | wires it together, runs the clock | loads config, presses go |
| **Conveyor** | `bt.feeds` | feeds bars in time order | AVXL's 5-min candles, one by one |
| **Worker** | `Strategy.next()` | look at bars, decide buy/sell | "is a cup-and-handle done? place a buy-stop" |
| **Cashier** | `broker` | fills orders, charges **commission + slippage**, tracks cash | fills AVXL at $29.79, books the fee |
| **Inspector** | `analyzers` | measures the result | counts wins, max drawdown |

**The headline you asked about:** `1-min data feed` + `strategy file` → (Cerebro runs the clock)
→ **simulated trade records.** That's the entire job of these 5 stations.

### 2.1 Cup-and-handle as a Backtrader strategy (commented)
```python
import backtrader as bt

class CupHandle(bt.Strategy):
    params = dict(rim_band=0.25, entry_off=0.01)     # the knobs

    def __init__(self):
        self.order = None                            # remember our open order

    def next(self):                                  # ← called once per bar
        if self.position or self.order:              # already in a trade? do nothing
            return
        sig = detect_cup_handle(self.data, self.p)   # YOUR rules, on bars so far
        if sig:                                      # a cup-and-handle just completed:
            self.order = self.buy_bracket(           # buy-stop entry + protective stop, atomically
                price=sig.entry, stopprice=sig.stop, exectype=bt.Order.Stop)

    def notify_trade(self, trade):                   # ← called when a trade closes
        if trade.isclosed:
            R = trade.pnl / sig.risk                 # record the result in R
            log_trade(trade, R)

cerebro = bt.Cerebro()
cerebro.adddata(bt.feeds.PandasData(dataname=avxl_5min,
                timeframe=bt.TimeFrame.Minutes, compression=5))
cerebro.addstrategy(CupHandle)
cerebro.broker.setcash(100_000)
cerebro.broker.setcommission(commission=0.0003)      # ← costs live HERE
cerebro.broker.set_slippage_fixed(0.02)              # ← and HERE (this is the honesty knob)
cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")
results = cerebro.run()
```
🔄 **Any strategy:** only `detect_cup_handle` changes. Swap it for `detect_breakout`,
`detect_mean_reversion`, anything — the 5 stations stay identical.

### 2.2 ⚠️ What bit us: we *didn't* use Backtrader — know why
Backtrader simulates **one exit rule per run**. We wanted the result at **every** take-profit
(1R…20R) from a **single** pass, to answer *"take 1R or hold for more?"* — so we wrote a custom
**full-path labeler** (replay once, record what each TP *would* have made).
- ✅ Win: one pass gives us the whole 1R–20R curve for AVXL.
- ⚠️ Cost we accepted: we gave up Backtrader's **portfolio** view (cash, many positions at once,
  drawdown). "+0.2R per trade" tells you nothing about your worst week if 50 signals fire the
  same morning. **Before going live, you still need the portfolio backtest.**

**Lesson:** match the engine to the *question*. Per-trade edge → custom labeler. Portfolio
risk/drawdown → Backtrader.

---

## 3. Our code = the loop made real

Every file is one station of the factory:

| our file | station | 🎯 what it does |
|---|---|---|
| `cup_coffee_config_v2.py` | Manager | all the knobs (cup 15–60 bars, handle 4–60, ±25% rim) |
| `data_layer.py` | Conveyor | `Bars` + downsample 1→2→5-min |
| `providers_historical.py` | Conveyor | find the day's **gapper** universe on Polygon |
| `research_data.py` | Conveyor | **cache** every API call + `label_full_path` + splits |
| `enrich_real_price.py` | Conveyor | recover the **real** price (un-split) — the cost fix |
| `pattern_detector.py` | Worker | the cup→handle→entry/stop rules |
| `backfill.py` | clock | universe → detect → label → append (resumable) |
| `feature_library.py`, `mine_factors.py` | Inspector++ | factors + scoring (data mining) |
| `app.py` | Inspector | the Streamlit dashboard you've been using |

### 3.1 Data mining (the "which signal helps?" search)
💡 **Plain English:** you have ~28 candidate clues (factors). Which ones actually predict a
better outcome? You *test* each — but testing many things invites false winners.
```
for each factor (e.g. "gap size", "time of day"), for each take-profit 1R..20R:
    split trades high-vs-low on the factor → compare net R/trade
    is the gap real, or luck?  (statistical test)
correct for testing 28×20 = 560 things  →  Benjamini-Hochberg (FDR)
safety net: score RANDOM fake factors too. If a fake "wins," your bar is too low.
```
⚠️ **What bit us is what this prevents:** test 560 things and ~28 pass by pure chance. A beginner
reports those as "signals." FDR + the fake-factor canary are the seatbelt.

---

## 4. Scoring the result (QuantStats)

### 4.1 The conversion you must not skip: R → returns
💡 **Plain English:** R is *risk-relative*; QuantStats wants *account-relative* % returns. Bridge
them by choosing **how much you risk per trade.**

🎯 **AVXL, made concrete:** decide you risk **1% of your account per trade**. Then AVXL's +4R
result = **+4%** of the account. Do that for every trade → build an equity curve → take daily
% changes → hand to QuantStats:
```python
RISK_PER_TRADE = 0.01                       # 1 R = +1% of equity
trade_returns  = r_multiples * RISK_PER_TRADE
equity = (1 + pd.Series(trade_returns, index=trade_dates)).cumprod()
daily  = equity.resample('1D').last().pct_change().dropna()

import quantstats as qs
qs.reports.html(daily, output="cupcoffee.html", title="Cup Coffee")   # full tearsheet
```
⚠️ **What bit us in the chat:** "+539R over 5 years" is **not a return** until you pick the
risk-per-trade rule. 539R at 1%/trade is a very different account than at 0.25%/trade.

### 4.2 The report card (what each number means)
| metric | 💡 plain English | rough "good" |
|---|---|---|
| **CAGR** | yearly growth rate | beat the benchmark |
| **Max Drawdown** | worst peak-to-valley fall | the pain you must stomach |
| **Sharpe** | reward per unit of total bumpiness | >1 ok, >2 good |
| **Sortino** | reward per unit of *downside* bumpiness | ≥ Sharpe |
| **Calmar (卡玛)** | CAGR ÷ max drawdown | >1 = you earn more/yr than your worst drop |
| **Expectancy** | avg net R per trade | **>0 after costs is the whole game** |

🎯 Our Streamlit dashboard is a hand-rolled QuantStats: the cumulative-R equity curve, per-year
and per-session R, and the **cost-vs-return** slider that exposed the reverse-split bug.

---

## 5. Making it better (the optimization ladder)

Climb **in order**. Each rung adds power *and* overfitting risk — so **validate (5.4) before the
next rung.**

| rung | 💡 plain English | 🎯 cup-and-handle | ⚠️ trap |
|---|---|---|---|
| **1 → multi signal** | confirm with a 2nd, *unrelated* clue | breakout-timing **+** a regime filter | two versions of the same idea ≠ diversification |
| **fixed → adaptive** | let a knob follow the market | stop = a multiple of **current volatility (ATR)**, not a fixed % | more knobs = easier to overfit; keep it to one |
| **fixed → dynamic size** | bet more/less by risk | risk a constant **% of equity** per trade (vol targeting) | sizing drives drawdown more than entries do |
| **validate** | prove it's not luck | below ↓ | skipping this = believing a curve-fit |

### 5.4 蒙特卡洛 (Monte Carlo) + 参数稳定性 (parameter stability) — the BS detector
- **Walk-forward:** tune on old data, test on *newer untouched* data, roll forward. Only the
  untouched results count.
- **Monte Carlo:** shuffle the trade order 10,000 times → a *distribution* of drawdowns. Your one
  backtest is just *one* path; you want to know the unlucky paths too.
- **Parameter stability:** plot results across a *grid* of a knob. You want a broad **plateau**,
  not a lone spike. A setting that only works at exactly 0.25 (and dies at 0.24/0.26) is fit to noise.
- ⚠️ **Our real war story:** a "rolling ratchet" tweak looked great on a clean **450-day** sample
  (+0.34 vs +0.52). On the **full 5 years** it backtested *worse*. The pretty short window lied.
  **Always trust the full-period, out-of-sample result over a clean slice.** (Now a frozen rule.)

---

## 6. Going live (paper → real money)

### 6.1 The staircase — never skip a step
```
backtest → walk-forward → PAPER (sim, live data/fake money) → small live → scale
```
Most strategies die at "paper" — real slippage and partial fills the backtest never modeled.

### 6.2 The live system = your 行情/交易/策略/数据/监控
What you described **is** the architecture of **vnpy**, the standard Chinese open-source framework:

| your term | module | 💡 job |
|---|---|---|
| **行情** market data | gateway feed | real-time prices in |
| **交易** trading | order manager | orders out, fills + positions back |
| **策略** strategy | engine | runs your `next()` on live bars |
| **数据** data | recorder/DB | store ticks/trades for replay & audit |
| **监控** monitoring | risk + GUI | limits, alerts, **kill-switch** |

**CTP** = the dominant **China-futures** trading API. **SimNow** = its free **paper-trading**
sandbox (`simnow.com.cn`). Path: write a vnpy strategy → point its CTP gateway at **SimNow** →
paper-trade → later swap SimNow's address for your live broker's.

### 6.3 ⚠️⚠️ The fork you MUST notice before writing any live code
**CTP / SimNow / vnpy trade *Chinese futures only*. They cannot trade US stocks.** Our whole
project is **US-equity gappers (Polygon)**. So:
- **Take *this* strategy live** → a **US broker** API (Interactive Brokers / Alpaca), *not* CTP.
  Same 5 modules, different gateway.
- **Use CTP/SimNow** → you're building a **different** strategy on China futures — new universe,
  new data, new costs, re-validate from scratch.

Pick the market **first** — it decides your data feed, cost model, broker, and paperwork.

---

## 7. The 6 scars (paste these on the wall)

1. **Split-adjusted prices lie about cost.** DBGI shown at $678k was really ~$6 (3 reverse splits,
   ×125,000). A fixed ¢/share fee then looks free when it's brutal. → recover the **real** price,
   cost at it, screen out sub-$15 names.
2. **The tiny-stop tax.** XELA: real $2.08, **1¢ stop**, **2.6¢ fee** = **2.6R cost per trade** —
   the fee was bigger than the bet. Always write cost as `fee / (entry − stop)` and check the
   *distribution*, not just the average.
3. **Look-ahead & survivorship.** Every factor must be **point-in-time** — filter on what you knew
   *that day*, never on what happened later.
4. **Net, always.** Gross results are marketing. Only **net of commission + slippage at the real
   price** counts.
5. **Freeze your evaluation.** Once the label/exit/metric is set, don't re-slice it to flatter a
   result. (We froze: full-path labels, fixed 1R–5R grid, index kept in.)
6. **Distrust the clean number.** Integer `@R` values, a too-pretty 450-day curve, 14% of profit
   paying ~0 cost — each "too clean" thing was a **bug**. When a number looks clean, **verify it.**

---

## CAPSTONE: AVXL through all 7 stages (the whole loop in one trade)

| # | stage | 🎯 what happens to AVXL | the number |
|---|---|---|---|
| ① | **idea** | "gappers that form a cup-and-handle may run" | — |
| ② | **data** | pull AVXL's 1-min bars for 2021-06-28, cache, downsample to 5-min | ~78 five-min bars |
| ③ | **detect** | a cup (U-shape) then a handle completes; right rim ≈ $29.78 | **entry $29.79, stop $29.43, R = $0.36** |
| ④ | **backtest** | replay forward: it climbs, peaks, falls back | **MFE 4.8R**; realized @1R=+1, @2R=+2, @3R=+3, **@4R=+4**, **@5R=−1** |
| ⑤ | **cost** | risk$ $0.36 → cost = $0.026 / $0.36 = **0.072R**; real price $29.79 ≥ $15 ✅ survives screen | **net @4R = +3.93R** |
| ⑥ | **analyze** | this +3.93R drops into the equity curve, the 2021 bucket, the 5-min / mid-tier slices | one green brick in the wall |
| ⑦ | **lesson** | had we set TP **5R**, this **winner flips to a −1.07R loser** (it turned at 4.8R) | **this is *why the project exists*** |

That last row is the soul of Cup Coffee: the *same trade* is +4R or −1R purely by the exit rule —
so the research question was never "is cup-and-handle real?" but **"where do you take profit?"**

---

## Your next-strategy checklist (swap the strategy, keep the skeleton)

- [ ] Write the **one question** the backtest must answer (ours: *take 1R or hold?*).
- [ ] Pull data → **cache it** → clean it → **eyeball raw prices** (this is where splits hide).
- [ ] Pick the engine for the question (per-trade labeler vs Backtrader portfolio).
- [ ] Build the **cost model first**; express everything **net, in R, at real prices**.
- [ ] Mine factors with **FDR + a fake-factor canary**; keep only point-in-time clues.
- [ ] Report with QuantStats (convert R→returns via a risk-per-trade rule).
- [ ] Climb the ladder; **validate each rung** (walk-forward + Monte Carlo + plateau).
- [ ] Decide **market & broker** (US equity → IBKR/Alpaca; China futures → CTP/SimNow/vnpy).
- [ ] Paper trade before one real dollar; monitor with a kill-switch.
- [ ] Keep a **"too clean = verify"** log of every surprising number.
```
