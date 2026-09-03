"""
live_trader_ibkr.py — the ORDER ROBOT (IBKR paper): PRE-ARM the bracket during the handle
==========================================================================================
Streams IBKR bars into the same look-ahead-free rules as the backtest, and places the resting
buy-stop BEFORE the breakout — while the handle is still forming — exactly as the backtest assumes.

MULTI-TIMEFRAME: the backtest pile is 1min + 2min + 5min TOGETHER, so the live bot runs all three
(--tfs). Only ONE data stream per symbol is used: the 1-min stream is aggregated locally into
2min/5min bars (clock-aligned from 09:30) — no extra IBKR data lines, no extra lag. VERIFIED vs
the backtest's cached Polygon bars: 100% identical bars+signals on full-coverage days; on thin
names with missing minutes Polygon's own 2/5min files re-anchor windows after gaps (a vendor
quirk, not reproducible live), so ~88% of signals reproduce exactly and most of the rest are the
same setup phase-shifted a few minutes. Deterministic clock alignment is the correct live choice.
Non-backtested timeframes (e.g. 3min) are allowed but warned: NO EVIDENCE.

WHY PRE-ARM WORKS (timing): a cup's right rim only counts as a peak one bar AFTER it prints, and
the earliest legal entry is at least one bar after THAT — so there is always >=1 closed bar to get
the resting order in before the earliest possible break. A breakout then fills at the FIRST TOUCH
of rim+$0.01 (plus real spread/gap-through), not at wherever the bar happened to close.

LIFECYCLE per setup:
  PRE-ARM   valid cup + forming handle + next bar may legally enter -> place native IBKR bracket:
            BUY-STOP parent @ rim+$0.01, OCA children: take-profit LMT @ +tp*R, stop-loss STP @ handle low
  UPDATE    each closed bar while forming: handle low drifts down -> cancel + re-place the (unfilled)
            bracket under a NEW ref (IBKR 10326: OCA children cannot be revised in place)
  CANCEL    handle invalidates (deeper than 20% of cup, or too old) -> cancel the pending bracket
  FILL      logged to data/paper_fills.csv with slippage vs the intended level  <- THE measurement.
            ARMED "entered" = what IBKR FILLED (never what the bar looked like): a breakout bar with
            no fill gets one bar of grace, then the bracket is cancelled + logged as MISSED FILL.
  WATCHDOG  every 30s: every share on OUR book has a working stop, no exit is larger than the book,
            a flat book has no exit orders left (own-book only — never the account view)
  FLATTEN   EOD 15:49 or Ctrl-C -> cancel MY orders + close MY book (one market close per ref)

ONE PENDING PER SYMBOL across all timeframes — the live equivalent of the backtest's cross-
timeframe dedup, and the guard against the same breakout being bought twice on 1min AND 5min.

SAFETY: SHADOW by default (--arm to actually place PAPER orders) · refuses live ports (4001/7496) ·
EOD flatten · kill-switch · idempotent per setup · --max-positions cap.

    python live_trader_ibkr.py --delayed                 # SHADOW on free delayed data — safe first run
    python live_trader_ibkr.py NVDA,AMD,SMCI             # SHADOW, real-time data
    python live_trader_ibkr.py NVDA,AMD --arm            # actually place PAPER orders (tp default 6R)
    python live_trader_ibkr.py NVDA --tfs 1min,5min      # choose timeframes (default 1min,2min,5min)
Needs IB Gateway/TWS running in PAPER with the API enabled (Gateway paper port 4002).
"""
from __future__ import annotations
import os, sys, argparse
from datetime import time as dtime, datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

from pattern_detector import PatternDetector, _is_peak
from cup_coffee_config_v2 import CONFIG
from data_layer import Bars

ET = ZoneInfo("America/New_York")
DEFAULT_SYMS = ["SPY","QQQ"]
DEFAULT_TFS = "15s"                # THE PROGRAM (2026-09-02): 15-second cup-and-handle, forward test.
                                   # legacy minute mode still works: --tfs 1min,2min,5min
BACKTESTED_TFS = {"1min", "2min", "5min"}
EOD = dtime(15, 49)
LIVE_PORTS = {4001, 7496}
FILLS_CSV = "data/paper_fills.csv"
DONE_STATES = ("Filled", "Cancelled", "ApiCancelled", "Inactive")
OPEN_MIN = 9 * 60 + 30                  # 09:30 in minutes — aggregation anchor


def resolve_watchlist(path: str) -> str | None:
    """Turn --watchlist into a real file. 'auto' (the default) picks the NEWEST
    *DayTrade*.txt in ~/Downloads — so a TradingView export named '8_21_2026 DayTrade.txt'
    is found without typing the date. Falls back to data/watchlist.txt. Pure local
    filesystem lookup: no network, nothing that can move under us."""
    import glob
    if path and path != "auto":
        return path if os.path.exists(path) else None
    # Recognize a TradingView export by its CONTENT, never its filename: the user renames
    # the watchlist daily (2026-08-26 "daytrade" lowercase, 2026-09-02 "9_2_2026.txt" with
    # no keyword at all) and every filename rule eventually fails silently into the stale
    # data/watchlist.txt. Newest export by modification time wins.
    cands = [c for c in glob.glob(os.path.expanduser("~/Downloads/*.txt")) if is_tv_export(c)]
    if cands:
        return max(cands, key=os.path.getmtime)
    return "data/watchlist.txt" if os.path.exists("data/watchlist.txt") else None


def is_tv_export(path: str) -> bool:
    """A TradingView watchlist export: a small text file whose fields are ###SECTION
    headers and EXCHANGE:TICKER tokens (NASDAQ:NVDA, AMEX:SPY, NYSE:DE ...)."""
    try:
        if os.path.getsize(path) > 200_000:
            return False
        raw = open(path, errors="ignore").read(20_000)
    except OSError:
        return False
    fields = [f.strip() for f in raw.replace("\n", ",").split(",") if f.strip()]
    if not fields:
        return False
    tagged = sum(1 for f in fields if f.startswith("###")
                 or (":" in f and f.split(":")[0].isalpha() and f.split(":")[0].isupper()))
    return tagged >= max(2, len(fields) // 2)


def watchlist_additions(known: set[str], path: str | None, today) -> list[str]:
    """HOT-ADD (user need 2026-09-02: The Fly publishes at 09:55, after the open).
    Given the symbols already running and the newest export on disk, return the NEW
    tickers to bring online — only from an export saved TODAY (a stale file must never
    inject names), and never removals: a symbol may have an open position, so the
    running set only grows during a session."""
    if not path or not os.path.exists(path):
        return []
    if datetime.fromtimestamp(os.path.getmtime(path), ET).date() != today:
        return []
    syms, _ = read_watchlist(None, path)
    return [x for x in syms if x not in known]


class _CachedBar:
    """Looks enough like an ib_async BarData for ingest(): .date (tz-aware), o/h/l/c/volume."""
    __slots__ = ("date", "open", "high", "low", "close", "volume")
    def __init__(self, r):
        self.date = datetime.fromtimestamp(r["t"] / 1000, ET)
        self.open, self.high, self.low, self.close, self.volume = r["o"], r["h"], r["l"], r["c"], r.get("v", 0)


def cached_day_bars(sym: str, day: str):
    """The day's 15s bars from cache/ibkr15s (cache_15s.py / record_day.py write them), or None."""
    import json
    p = f"cache/ibkr15s/{sym}/{day}.json"
    if not os.path.exists(p):
        return None
    rows = json.load(open(p))
    return [_CachedBar(r) for r in rows] + [_CachedBar(rows[-1])] if rows else None   # +1: ingest drops the forming last bar


MIN_PRICE = 15.0
# THE OLD UNIVERSE RULES, applied live to every ticker the watchlist hands us (user 2026-09-02).
# IBKR contract details carry the classification (industry / category / stockType).
COMMODITY_INDUSTRIES = {"Basic Materials"}                       # mining, steel, chemicals, forest
COMMODITY_CATEGORIES = {"Oil&Gas", "Oil&Gas Services", "Coal", "Pipelines", "Mining", "Iron/Steel",
                        "Chemicals", "Forest Products&Paper", "Agriculture", "Metal Fabricate/Hardware"}


def universe_verdict(price: float | None, details) -> str:
    """'' if tradable, else the reason it is dropped. Rules: common stock only (no ETF/ETN/
    fund), price >= $15, not commodity-related (oil/gas, coal, mining, metals, steel,
    chemicals, agriculture). Every drop is recorded, never silent."""
    st = (getattr(details, "stockType", "") or "").upper()
    if st and st not in ("COMMON", "ADR"):
        return f"not common stock ({st})"
    if price is not None and price < MIN_PRICE:
        return f"price ${price:.2f} < ${MIN_PRICE:.0f} floor"
    ind = getattr(details, "industry", "") or ""
    cat = getattr(details, "category", "") or ""
    if ind in COMMODITY_INDUSTRIES or cat in COMMODITY_CATEGORIES:
        return f"commodity-related ({ind} / {cat})"
    return ""


def record_universe(day: str, rows: list[dict]) -> None:
    """Merge rows into data/universe_log.csv by (day, symbol) — the live bot's screen
    verdicts join record_day.py's history verdicts in one file."""
    import csv
    path = "data/universe_log.csv"
    cols = ["day", "symbol", "sources", "n_sources", "open", "qualified", "reason"]
    old = []
    if os.path.exists(path):
        keys = {(r["day"], r["symbol"]) for r in rows}
        old = [r for r in csv.DictReader(open(path)) if (r["day"], r["symbol"]) not in keys]
    os.makedirs("data", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", restval="")
        w.writeheader()
        for r in sorted(old + rows, key=lambda x: (x["day"], x["symbol"])):
            w.writerow(r)


def read_watchlist(cli_symbols: str | None, path: str) -> tuple[list[str], str]:
    """Where the day's tickers come from, in priority order:
         1. symbols typed on the command line
         2. the --watchlist text file (one per line OR comma-separated)
         3. the built-in DEFAULT_SYMS
    Deliberately dependency-free: no network, no parsing of anything that can move.
    Worst case it behaves exactly as the hardcoded list did, so it cannot add a failure mode.
    Blank lines and '# comments' are ignored; exchange prefixes (NASDAQ:AAPL) are stripped;
    duplicates are removed while preserving order."""
    def clean(raw: str) -> list[str]:
        """Accepts a raw TradingView watchlist export verbatim, or hand-typed tickers.
        TradingView format: one long comma-separated line where '###SECTION NAME' marks a
        group header (and the name may contain spaces) — so split on COMMAS first, then drop
        any field beginning with '###'. Our own notes use '# ...' at the start of a line."""
        out, seen, junk = [], set(), []
        for line in raw.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#") and not s.startswith("###"):
                continue                                    # our own comment line
            for field in s.split(","):
                field = field.strip()
                if not field or field.startswith("###"):
                    continue                                # TradingView section header
                if field.startswith("#"):
                    break                                   # trailing comment -> rest of line is a note
                for tok in field.split():                   # also allow space-separated tickers
                    t = tok.strip().upper().split(":")[-1]  # NASDAQ:SHOP / TVC:VIX -> SHOP / VIX
                    if not t:
                        continue
                    if len(t) > 6 or not t.replace(".", "").replace("-", "").isalnum():
                        junk.append(tok)                    # section words, stray prose, etc.
                        continue
                    if t not in seen:
                        seen.add(t)
                        out.append(t)
        if junk:
            print(f"⚠️ watchlist: ignored {len(junk)} non-ticker token(s): {', '.join(junk[:6])}")
        return out

    if cli_symbols:
        return clean(cli_symbols), "command line"
    resolved = resolve_watchlist(path)
    if resolved:
        syms = clean(open(resolved, errors="ignore").read())
        if syms and resolved == "data/watchlist.txt":
            print("\n" + "!" * 78 + "\n!!  NO TradingView export found in ~/Downloads — using the STALE fallback\n"
                  "!!  data/watchlist.txt. If you exported today, the file was not recognized:\n"
                  "!!  Ctrl-C now and pass it explicitly:  --watchlist ~/Downloads/<file>.txt\n" + "!" * 78 + "\n")
        if syms:
            age = (datetime.now().timestamp() - os.path.getmtime(resolved)) / 3600
            stale = f"  ⚠️ {age/24:.0f} days old — re-export?" if age > 20 else ""
            return syms, f"{resolved}{stale}"
        print(f"⚠️ {resolved} has no tickers — falling back to the built-in list")
    else:
        print(f"⚠️ no watchlist found (looked for ~/Downloads/*DayTrade*.txt and "
              f"data/watchlist.txt) — falling back to the built-in list")
    return list(DEFAULT_SYMS), "built-in DEFAULT_SYMS"


def archive_watchlist(path: str | None) -> None:
    """THE POINT-IN-TIME UNIVERSE LOG (pivot decision 2026-09-02). The manual
    news-source watchlist is the one input that cannot be reconstructed later —
    so every trading day's export is copied to data/watchlists/YYYY-MM-DD.txt
    (source ###SECTION tags preserved verbatim). Idempotent: the newest export
    of the day wins; tiny text files, committed to git for free backup."""
    if not path or not os.path.exists(path):
        return
    if datetime.fromtimestamp(os.path.getmtime(path), ET).date() != datetime.now(ET).date():
        print("  📚 watchlist NOT archived — the export is from an earlier day (a stale list "
              "must never enter the point-in-time log; re-export from TradingView)")
        return
    os.makedirs("data/watchlists", exist_ok=True)
    dst = f"data/watchlists/{datetime.now(ET):%Y-%m-%d}.txt"
    content = open(path, errors="ignore").read()
    if os.path.exists(dst) and open(dst, errors="ignore").read() == content:
        return
    with open(dst, "w") as f:
        f.write(content)
    print(f"  📚 watchlist archived -> {dst}")


def scan_setups(det: PatternDetector, b: Bars, memo: dict | None = None) -> dict[int, dict]:
    """Every cup setup and the CURRENT state of its handle, keyed by right-rim index.

    Mirrors _find_cup + _find_handle EXACTLY (same gates, same walk) but does not require the
    entry bar to exist yet — the stage the frozen detector never exposes:
      forming  — handle still valid, no break yet. trigger/stop are live values; armable once
                 the NEXT bar index (len(b)) >= earliest (so a resting order can never fill on
                 a bar the backtest would have rejected).
      entered  — bar `entry_bar` broke the trigger (the detector's entry bar; resting order filled).
      dead     — handle got deeper than 20% of the cup, or ran past handle_max bars: give up.
    """
    n = len(b)
    out: dict[int, dict] = {}
    # SPEED (2026-09-02): a left rim's outcome is FINAL once its cup+handle is dead, entered,
    # or symmetry-failed — later bars cannot change bars that already printed. `memo`
    # (per symbol-day, owned by the caller) remembers those, so each new bar re-examines
    # only rims that are still forming or still cup-less. Results are identical to the
    # full scan (tests prove it on a real day); the replay went from ~45 min to seconds.
    for li in range(1, n - 1):
        if memo is not None and li in memo:
            fin = memo[li]
            if fin is not None:
                ri_f, st_f = fin
                if ri_f not in out:
                    out[ri_f] = st_f
            continue
        if not _is_peak(b, li):
            if memo is not None and li < n - 2:        # peak-ness of an interior bar never changes
                memo[li] = None
            continue
        min_ri = 0
        while True:                                    # ROLLING RIM (user spec 2026-07-20): a pre-entry
            if memo is not None and min_ri == 0:       # bar above the rim dethrones it -> re-search for
                cup = _find_cup_resumable(det, b, li, memo)   # the next peak-confirmed rim that re-passes gates
            else:
                cup = det._find_cup(b, li, min_ri)
            if cup is None:
                break
            bottom_idx, ri = cup
            cup_low = b.l[bottom_idx]
            rim, lip = b.h[ri], b.h[li]
            depth_h = rim - cup_low
            if depth_h <= 0:
                if memo is not None:
                    memo[li] = None                    # final: this rim never yields a cup
                break
            _d = min if det.rim_mode == "min" else max     # rim symmetry, same as _find_handle
            if abs(rim - lip) >= det.rim_recov * _d(lip - cup_low, rim - cup_low):
                if memo is not None:
                    memo[li] = None                    # final: symmetry fail on its first valid rim
                break                                  # symmetry fail = this li is done (detector parity)
            trigger = rim + det.entry_off
            # USER SPEC 2026-07-23: no momentum fast-lane under the rolling rim — 4-bar floor always
            momentum = (not det.rim_roll) and b.h[ri + 1] >= rim - 1e-9
            earliest = max((ri + 1) if momentum else (ri + det.h_min - 1), ri + 2)
            state, entry_bar, hl, rolled = None, None, float("inf"), False
            walk_end = min(n, ri + det.h_max) if det.h_max else n
            for k in range(ri + 1, walk_end):                 # the detector's exact handle walk
                if k >= earliest and hl < float("inf") and b.h[k] >= trigger:
                    state, entry_bar = "entered", k
                    break
                if det.rim_roll and b.h[k] > rim + 1e-9:
                    rolled, min_ri = True, k           # rim dethroned -> roll
                    break
                hl = min(hl, b.l[k])
                if rim - hl > det.h_depth_frac * depth_h:
                    state = "dead"
                    break
            if rolled:
                continue
            if state is None:                          # walk ran out of bars
                state = "dead" if (det.h_max and n >= ri + det.h_max) else "forming"
            st = dict(state=state, trigger=trigger, stop=hl, earliest=earliest,
                      entry_bar=entry_bar, momentum=momentum)
            if ri not in out:                          # first successful li wins (detector order)
                out[ri] = st
            if memo is not None and state in ("dead", "entered"):
                memo[li] = (ri, st)                    # final: printed bars cannot un-die or un-enter
            break
    return out


def _find_cup_resumable(det: PatternDetector, b: Bars, li: int, memo: dict):
    """det._find_cup(b, li, 0), resumed bar to bar. For a left rim with no valid right rim
    yet, the only right-rim candidate a NEW bar can add is ri = n-2 (a bar is peak-eligible
    only once its right neighbour exists); every earlier ri was rejected on bars that have
    not changed, and the running cup bottom is carried along. Identical output to the full
    scan — the equivalence test proves it on a real day."""
    key = ("cup", li)
    st = memo.get(key)
    n = len(b)
    if st is None:                                     # first look: run the real thing once
        cup = det._find_cup(b, li, 0)
        if cup is not None:
            memo[key] = ("found", cup)
            return cup
        # nothing yet: remember the interior bottom over (li, n-2] and where to resume
        lo, lo_i = float("inf"), li
        for j in range(li + 1, n - 1):
            if b.l[j] < lo:
                lo, lo_i = b.l[j], j
        memo[key] = ("none", n - 1, lo, lo_i)         # next ri to examine = n-1 (eligible next bar)
        return None
    if st[0] == "found":
        return st[1]
    _, next_ri, lo, lo_i = st
    left_high = b.h[li]
    cup = None
    for ri in range(next_ri, n - 1):                   # only the newly eligible right rims
        j = ri - 1
        if j >= li + 1 and b.l[j] < lo:
            lo, lo_i = b.l[j], j
        if ri - li < det.cup_min or not _is_peak(b, ri):
            continue
        depth = left_high - lo
        if depth <= 0:
            continue
        rh = b.h[ri]
        if rh < left_high - det.rim_recov * depth or rh > left_high + det.rim_recov * depth:
            continue
        if det._obstructed(b, li, left_high, ri, rh):
            continue
        cup = (lo_i, ri)
        break
    if cup is not None:
        memo[key] = ("found", cup)
        return cup
    memo[key] = ("none", n - 1, lo, lo_i)
    return None


class MinuteAggregator:
    """Incremental 1min -> k-min bars, clock-aligned buckets from 09:30 (same as the backtest's
    2min/5min bars). Calls sink(ts, o, h, l, c, v) the moment a bucket CLOSES — either its final
    constituent minute closed, or a bar from a later bucket arrived (gap in thin names)."""
    def __init__(self, k: int, sink):
        self.k, self.sink, self.cur = k, sink, None

    def add(self, t, o, h, l, c, v):
        m = (t.hour * 60 + t.minute - OPEN_MIN) // self.k
        if self.cur and (self.cur["date"] != t.date() or self.cur["m"] != m):
            self._flush()                              # previous bucket closed by gap / new bucket
        if self.cur is None:
            mm = OPEN_MIN + m * self.k
            self.cur = dict(date=t.date(), m=m, ts=t.replace(hour=mm // 60, minute=mm % 60),
                            o=o, h=h, l=l, c=c, v=v)
        else:
            cu = self.cur
            cu["h"] = max(cu["h"], h); cu["l"] = min(cu["l"], l); cu["c"] = c; cu["v"] += v
        if (t.hour * 60 + t.minute - OPEN_MIN) % self.k == self.k - 1:
            self._flush()                              # final minute of the bucket just closed
    def _flush(self):
        if self.cur:
            bu, self.cur = self.cur, None
            self.sink(bu["ts"], bu["o"], bu["h"], bu["l"], bu["c"], bu["v"])


class TickBarBuilder:
    """Delayed-tick -> 1min bars. WHY THIS EXISTS: IBKR's free delayed tier serves HISTORICAL
    bars only for COMPLETED sessions — today's bars are invisible until the close, and
    keepUpToDate never delivers on delayed. But delayed STREAMING ticks DO flow, so we build
    today's 1-min bars ourselves. A minute's bar closes when the first tick of the next minute
    arrives. Volume = diff of the cumulative day volume (units don't matter — detection uses
    only prices). Timestamps are wall-clock minus the 15-min delay ≈ the bar's market time."""
    def __init__(self, sink):
        self.sink = sink; self.cur = None; self.lastvol = None

    def on_tick(self, t, price, cumvol):
        m = t.replace(second=0, microsecond=0)
        if self.cur and self.cur["t"] != m:
            b, self.cur = self.cur, None
            self.sink(b["t"], b["o"], b["h"], b["l"], b["c"], b["v"])
        if price is None or price != price or price <= 0:
            return
        if self.cur is None:
            self.cur = dict(t=m, o=price, h=price, l=price, c=price, v=0.0)
        else:
            cu = self.cur
            cu["h"] = max(cu["h"], price); cu["l"] = min(cu["l"], price); cu["c"] = price
        if cumvol is not None and cumvol == cumvol:
            if self.lastvol is not None and cumvol > self.lastvol and self.cur is not None:
                self.cur["v"] += cumvol - self.lastvol
            self.lastvol = cumvol


class Trader:
    def __init__(self, ib, Order, MarketOrder, args, agg_ks, base_tf="1min"):
        self.ib, self.Order, self.MarketOrder, self.a = ib, Order, MarketOrder, args
        self.base_tf = base_tf                         # "15s" (the program) or "1min" (legacy)
        self.det = PatternDetector(CONFIG)
        self.agg_ks = agg_ks                           # e.g. [2, 5]
        self.aggs: dict[tuple, MinuteAggregator] = {}  # (sym, k) -> aggregator
        self.store: dict[tuple, Bars] = {}             # (sym, tf) -> closed-bar series
        self.fired: dict[tuple, set] = defaultdict(set)
        self.pending: dict[str, dict] = {}             # sym -> the ONE tracked pending setup (any tf)
        self.entered_at: dict[tuple, int] = {}         # (sym, tf) -> bar idx our pre-armed order filled
        self.seen: set[str] = set()                    # once-only log guard (skips etc.)
        self.contracts: dict[str, object] = {}
        self.eod_done = False
        self._report = False                           # current ingest context (seed vs live)
        self.eq0 = None                                # real NetLiq snapshot at startup (--base mode)
        self.live_started = False                      # first live bar announced?
        self.last_beat = None                          # heartbeat timestamp
        self._live1 = 0                                # live 1min bars received today
        self.cur_day = None                            # current session date (resets eod_done per day)
        self.paper: list = []                          # shadow-mode simulated open positions (exit narration)
        self.closed: list = []                         # today's completed trades (board + replay summary)
        self.open_real: dict = {}                      # armed mode: orderRef -> open position (from fills)
        self.brackets: dict = {}                       # orderRef -> bracket levels (for fill bookkeeping)
        self._last_t = None                            # latest live bar content time (board header)
        self._drawn = 0.0                              # board redraw throttle
        self._last_flatten = 0.0                       # flatten idempotency window
        self._memo: dict[tuple, dict] = {}             # (sym, tf) -> scan_setups memo for the current day
        self._inflight: dict = {}                      # ref -> working close order (never double-close)
        self._parent_trades: dict = {}                 # parent orderId -> Trade: is a child LIVE (parent filled)?
        self._superseded: dict = {}                    # old ref -> new ref after a cancel + re-place (race guard)
        self.blocked: set = set()                      # symbols the universe screen dropped: never fed, never armed
        self.unscreened: set = set()                   # started pre-open: the $15 rule waits for today's first bar
        self.universe_hook = None                      # main() installs: (sym, open_price) -> screen it now
        self._acct_noted: set = set()                  # (sym, qty) account-vs-book differences already reported
        self.log_path = f"logs/live_{datetime.now(ET):%Y-%m-%d}.log"

    # ---- account helpers -------------------------------------------------
    def equity(self) -> float:
        """Sizing equity. With --base N: a VIRTUAL account of $N that compounds with this
        session's P&L (base + NetLiq-change since startup) — so the $1M IBKR paper default
        sizes like the real $1k plan. Without --base: the account's actual NetLiquidation."""
        nl = 0.0
        for v in self.ib.accountValues():
            if v.tag == "NetLiquidation" and v.currency == "USD":
                nl = float(v.value)
                break
        if self.a.base > 0:
            if self.eq0 is None and nl > 0:
                self.eq0 = nl
            return self.a.base + (nl - (self.eq0 if self.eq0 is not None else nl))
        return nl

    def occupied_syms(self) -> set:
        """Symbols THIS strategy is already committed to (open position or resting
        bracket). Counts only its OWN trades, deliberately not ib.positions().

        Why (2026-07-27): a second robot (live_trader_tightflag.py) now trades the
        same account and the same symbols. The account view cannot tell whose
        position is whose, so reading it made this bot treat the tight-flag robot's
        QQQ position as its own — refusing its own QQQ setups AND burning one of its
        max_positions slots, so it silently took fewer trades in every symbol.
        Own-book accounting is the per-strategy truth; each robot's exit orders carry
        their own share count, so the two never close each other's shares.
        Running ALONE this returns the same set as before (its own fills are exactly
        the account's positions)."""
        own = set(self.pending.keys())                 # a pending bracket holds a slot
        own |= {p["sym"] for p in self.paper}          # shadow-mode open positions
        own |= {v["sym"] for v in self.open_real.values()}   # armed-mode open positions
        return own

    # ---- output: REPLAY prints the line-by-line audit; LIVE keeps a clean status board ----
    def _say(self, line):
        """Lifecycle narration. Replay: print (the audit trail). Live: append to the session
        log file and refresh the status board — the terminal stays a clean dashboard."""
        if self.a.replay:
            print(line)
            return
        try:
            os.makedirs("logs", exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(line.strip() + "\n")
        except OSError:
            pass
        self._draw(force=True)

    def _draw(self, force=False):
        """Redraw the whole-screen status board: pending brackets, open positions with their
        stop/take-profit sitting next to them, and today's closed trades."""
        if self.a.replay or not self._report:
            return
        import time as _time
        if not force and _time.time() - self._drawn < 2:
            return
        self._drawn = _time.time()
        dayr = sum(c["r"] for c in self.closed)
        hdr_t = f"{self._last_t:%a %m-%d %H:%M}" if self._last_t else "waiting for bars"
        L = [f"CUP & HANDLE BOT — {'🔴 ARMED' if self.a.arm else '🟢 SHADOW'}  |  {hdr_t} (content time)  |  "
             f"{self._live1} live bars  |  sizing equity ${self.equity():,.0f}  |  day {dayr:+.2f}R",
             "═" * 108, "", "PENDING BRACKETS — buy-stop resting, waiting for the breakout"]
        if self.pending:
            L.append(f"  {'sym':<7}{'tf':<6}{'since':<8}{'trigger':>10}{'stop':>10}{'take-profit':>13}{'qty':>8}")
            for sym, p in self.pending.items():
                since = f"{p['ts']:%H:%M}" if p.get("ts") else "—"
                L.append(f"  {sym:<7}{p['tf']:<6}{since:<8}{p['trigger']:>10.2f}{p['stop']:>10.2f}"
                         f"{p['target']:>13.2f}{p['qty']:>8}")
        else:
            L.append("  none — scanning for setups…")
        L += ["", "OPEN POSITIONS — stop-loss & take-profit resting at IBKR" if self.a.arm
              else "OPEN POSITIONS (simulated) — would-be stop-loss & take-profit"]
        rows = ([(p["sym"], p["tf"], p["entry"], p["stop"], p["target"], p["trigger"])
                 for p in self.open_real.values()] if self.a.arm else
                [(p["sym"], p["tf"], p["entry"], p["stop"], p["target"], p["trigger"])
                 for p in self.paper])
        if rows:
            L.append(f"  {'sym':<7}{'tf':<6}{'entry':>10}{'stop':>10}{'take-profit':>13}{'last':>10}{'uP&L':>9}")
            for sym, tf, entry, stop, target, trig in rows:
                B = self.store.get((sym, self.base_tf))
                last = B.c[-1] if B and len(B) else entry
                risk = trig - stop
                ur = (last - entry) / risk if risk > 0 else 0.0
                L.append(f"  {sym:<7}{tf:<6}{entry:>10.2f}{stop:>10.2f}{target:>13.2f}{last:>10.2f}{ur:>+8.2f}R")
        else:
            L.append("  none")
        L += ["", "CLOSED TODAY"]
        if self.closed:
            for c in self.closed[-8:]:
                L.append(f"  {c['ts']:%H:%M}  {c['sym']:<7}{c['tf']:<6}{c['kind']:<5}@ {c['exit']:>9.2f}   {c['r']:+.2f}R")
        else:
            L.append("  none yet")
        L += ["", f"details -> {self.log_path} · fills -> {FILLS_CSV} · IBKR keeps the official record · Ctrl-C = flatten + quit"]
        sys.stdout.write("\x1b[2J\x1b[H" + "\n".join(L) + "\n")
        sys.stdout.flush()

    # ---- order plumbing ---------------------------------------------------
    def place_bracket(self, sym, pend):
        """Native IBKR bracket: STP parent + OCA (LMT take-profit / STP stop-loss) children.
        ocaType=2 (2026-09-02): a PARTIAL fill of one leg REDUCES the other proportionally.
        With ocaType=1 any fill cancels the sibling outright — ALMS 13:03 today: the TP
        filled 85 of 2,552 shares, IBKR cancelled the stop, and 2,467 shares rode
        unprotected from 10.55 to the 10.18 EOD flatten (-12.4R on a 3-cent risk)."""
        c = self.contracts[sym]
        pid = self.ib.client.getReqId()
        ref = pend["ref"]
        parent = self.Order(orderId=pid, action="BUY", orderType="STP", totalQuantity=pend["qty"],
                            auxPrice=pend["trigger"], tif="DAY", transmit=False, orderRef=ref)
        tp = self.Order(orderId=self.ib.client.getReqId(), action="SELL", orderType="LMT",
                        totalQuantity=pend["qty"], lmtPrice=pend["target"], tif="DAY", parentId=pid,
                        transmit=False, orderRef=ref, ocaGroup=ref, ocaType=2)
        sl = self.Order(orderId=self.ib.client.getReqId(), action="SELL", orderType="STP",
                        totalQuantity=pend["qty"], auxPrice=pend["stop"], tif="DAY", parentId=pid,
                        transmit=True, orderRef=ref, ocaGroup=ref, ocaType=2)
        trades = {}
        for name, o in (("ENTRY", parent), ("TP", tp), ("SL", sl)):
            tr = self.ib.placeOrder(c, o)
            tr.fillEvent += self._fill_logger(name)
            trades[name] = tr
        self._parent_trades[pid] = trades["ENTRY"]     # children are LIVE only once this parent has filled
        self.brackets[ref] = dict(pend)                # SNAPSHOT of the levels this ref was placed at (a
        return trades                                  # re-place gets a new ref; the old keeps its own)

    def _cancel_order(self, order) -> bool:
        try:
            self.ib.cancelOrder(order)
            return True
        except Exception as ex:
            self._say(f"  ⚠️ cancel failed for {getattr(order, 'orderRef', '?')}: {ex}")
            return False

    def _cancel_parent(self, pend, why) -> bool:
        """Cancel a pending bracket's parent (unfilled children die with it)."""
        ok = self._cancel_order(pend["trades"]["ENTRY"].order)
        if not ok:
            self._say(f"  ⚠️ {pend['ref']} could not be cancelled ({why}) — CHECK IBKR")
        return ok

    def _entry_filled(self, pend) -> tuple[int, int]:
        """(shares IBKR has filled on the entry, shares ordered) — the ONLY 'entered' that counts."""
        tr = pend["trades"]["ENTRY"]
        return int(getattr(tr.orderStatus, "filled", 0) or 0), int(pend["qty"])

    def _fill_logger(self, kind):
        """OWN-BOOK accounting from IBKR's fills — the only truth about what we hold. Every
        ref carries its share count: ENTRY adds, TP/SL/EOD subtract, and the trade is closed
        (tallied) only when the count reaches ZERO. A partial exit (ALMS 2026-09-02: the TP
        filled 85 of 2,552) leaves the rest on the book, where the watchdog keeps it stopped."""
        def on_fill(trade, fill):
            o = trade.order
            sym = trade.contract.symbol
            px = float(fill.execution.price)
            sh = int(fill.execution.shares)
            ref = o.orderRef
            if o.orderType == "MKT":                   # EOD / kill-switch close: cost vs the last known price
                B = self.store.get((sym, self.base_tf))
                level = B.c[-1] if B and len(B) else px
            else:
                level = o.lmtPrice if o.orderType == "LMT" else o.auxPrice
            slip = (px - level) if o.action == "BUY" else (level - px)   # + = worse than intended
            self._say(f"  💰 FILL {kind:5} {sym} {sh}@${px:.2f} (level ${level:.2f}, slip {slip*100:+.1f}¢)")
            br = self.brackets.get(ref, {})
            now = self._last_t or datetime.now(ET).replace(tzinfo=None)
            if kind == "ENTRY":                        # position opened (or grew) -> on the board
                pos = self.open_real.get(ref)
                if pos is None:
                    pos = self.open_real[ref] = dict(sym=sym, tf=br.get("tf", "?"), entry=px, qty=0,
                                                     cost=0.0, out=0.0, out_qty=0,
                                                     stop=br.get("stop", level), target=br.get("target", 0.0),
                                                     trigger=br.get("trigger", level), ts=now)
                pos["qty"] += sh
                pos["cost"] += sh * px
                pos["entry"] = pos["cost"] / pos["qty"]
                nxt = self._superseded.get(ref)
                if nxt:
                    # RACE (review 2026-09-02): this bracket was cancelled + re-placed for a deeper
                    # handle, but the fill beat the cancel. Its OCA exits are live at IBKR; the
                    # successor must never fill too (a double position).
                    pend = self.pending.get(sym)
                    if pend and pend["ref"] == nxt and pend["trades"]:
                        self._cancel_parent(pend, "successor of a bracket that filled during its re-place")
                        self.pending.pop(sym, None)
                    self._say(f"  🚨 {sym}: {ref} filled while being re-placed — successor {nxt} cancelled, "
                              f"the original OCA exits stand")
            elif kind in ("TP", "SL", "EOD"):          # position shrank -> closed only at zero
                pos = self.open_real.get(ref)
                if pos:
                    pos["qty"] -= sh
                    pos["out"] += sh * px
                    pos["out_qty"] += sh
                    if pos["qty"] <= 0:
                        self.open_real.pop(ref, None)
                        exit_px = pos["out"] / pos["out_qty"] if pos["out_qty"] else px
                        risk = pos["trigger"] - pos["stop"]
                        r = (exit_px - pos["entry"]) / risk if risk > 0 else 0.0
                        self.closed.append(dict(ts=now, sym=pos["sym"], tf=pos["tf"], kind=kind,
                                                exit=exit_px, r=r, entry=pos["entry"],
                                                trigger=pos["trigger"], stop=pos["stop"], entry_ts=pos["ts"]))
                    else:
                        self._say(f"  ↕️ {sym}: {sh} sh out on {kind}, {pos['qty']} sh still on the book "
                                  f"(stop ${pos['stop']:.2f} — the watchdog keeps it sized)")
            new = not os.path.exists(FILLS_CSV)
            with open(FILLS_CSV, "a") as f:
                if new:
                    f.write("time,symbol,ref,leg,action,shares,level,fill,slip_cents\n")
                f.write(f"{fill.time},{sym},{ref},{kind},{o.action},{sh},{level:.2f},{px:.2f},{slip*100:.1f}\n")
        return on_fill

    # ---- pending-setup lifecycle -------------------------------------------
    def arm_pending(self, sym, tf, ri, st, B):
        if sym in self.blocked:
            return
        ref = f"cuph-{sym}-{tf}-{B.date}-rim{ri}"
        trigger, stop = round(st["trigger"], 2), round(st["stop"], 2)
        risk = trigger - stop
        if risk <= 0:
            return
        stop_pct = risk / trigger * 100
        if self.a.minstop > 0 and stop_pct < self.a.minstop:
            # USER-APPROVED 2026-07-16 after three tiny-stop incidents (a $27M 1c-stop order, a
            # -4.79R gap-over on a 0.08% stop): the research pile's floor — setups with stops
            # thinner than this are sub-noise and were never part of the validated edge.
            if ref not in self.seen:
                self.seen.add(ref)
                self._say(f"  SKIP {sym} {tf} rim@{ri} — stop {stop_pct:.2f}% of price < "
                          f"{self.a.minstop:g}% floor (sub-noise; outside the validated pile)")
            return
        occ = self.occupied_syms()
        if sym in occ or len(occ) >= self.a.max_positions:
            if ref not in self.seen:
                self.seen.add(ref)
                self._say(f"  SKIP {sym} {tf} rim@{ri} — symbol occupied or at cap ({self.a.max_positions})")
            return
        eq = self.equity()
        qty = int((self.a.risk * eq) / risk) if eq > 0 else 0
        if qty < 1:
            if ref not in self.seen:
                self.seen.add(ref)
                self._say(f"  SKIP {sym} {tf} — size < 1 share (equity ${eq:,.0f}, risk/sh ${risk:.2f})")
            return
        target = round(trigger + self.a.tp * risk, 2)
        pend = dict(ri=ri, tf=tf, ref=ref, ref0=ref, gen=1, grace=0, trigger=trigger, stop=stop,
                    qty=qty, target=target, trades=None, ts=B.ts[-1])
        kind = "momentum" if st["momentum"] else "consolidation"
        line = (f"{sym} {tf} buy-stop ${trigger:.2f}  stop ${stop:.2f}  tp ${target:.2f} ({self.a.tp:g}R)  "
                f"{qty} sh  [{kind}]")
        if self.a.arm:
            pend["trades"] = self.place_bracket(sym, pend)
            self._say(f"  🛡️ PRE-ARMED   {B.ts[-1]:%m-%d %H:%M}  {line}")
        else:
            self._say(f"  🛡️ WOULD PRE-ARM {B.ts[-1]:%m-%d %H:%M}  {line}   [shadow]")
        self.pending[sym] = pend

    def update_pending(self, sym, st, B):
        pend = self.pending[sym]
        new_stop = round(st["stop"], 2)
        if new_stop > pend["stop"] - 0.005:            # stop only ratchets DOWN; ignore sub-cent noise
            return
        if pend["trades"] and self._entry_filled(pend)[0] > 0:
            return                                     # already (partly) ours: reconcile settles it, never re-place
        risk = pend["trigger"] - new_stop
        eq = self.equity()
        qty = max(1, int((self.a.risk * eq) / risk)) if eq > 0 else pend["qty"]
        pend.update(stop=new_stop, qty=qty, target=round(pend["trigger"] + self.a.tp * risk, 2))
        self._say(f"  🔧 {B.ts[-1]:%m-%d %H:%M}  {sym} {pend['tf']} handle deepened -> stop ${new_stop:.2f}, "
                  f"{qty} sh, tp ${pend['target']:.2f}")
        if pend["trades"]:
            # 2026-09-02: IBKR error 10326 "OCA group revision is not allowed" — a bracket's
            # children can NOT be modified in place (this is why the DUOL stop never moved on
            # 09-01). The bracket is unfilled here, so cancel it whole and re-place it under a
            # NEW ref (= a new OCA group): if the old parent fills inside the cancel's round
            # trip, the two brackets never share exits, and the fill logger kills the successor.
            old = pend["ref"]
            if not self._cancel_parent(pend, "re-place at the deeper stop"):
                return
            pend["gen"] += 1
            pend["ref"] = f"{pend['ref0']}-r{pend['gen']}"
            self._superseded[old] = pend["ref"]
            pend["trades"] = self.place_bracket(sym, pend)
            self._say(f"  🔁 bracket re-placed as {pend['ref']} (OCA cannot be revised)")

    def cancel_pending(self, sym, reason, ts=None):
        pend = self.pending.pop(sym)
        if pend["trades"]:
            self._cancel_parent(pend, reason)
        when = f"{ts:%m-%d %H:%M}  " if ts else ""
        self._say(f"  🗑️ CANCEL {when}{sym} {pend['tf']} pending bracket — {reason}")

    def reconcile(self, sym, tf, B):
        """Per closed bar of THIS timeframe: sync the symbol's one pending setup with the scanner."""
        if sym in self.blocked:
            return
        pend = self.pending.get(sym)
        if pend and pend["tf"] != tf:
            return                                     # slot held by another timeframe — cross-tf dedup
        scan = scan_setups(self.det, B, self._memo.setdefault((sym, tf), {}))
        n = len(B)
        if pend:
            st = scan.get(pend["ri"])
            bar_entered = st is not None and st["state"] == "entered"
            if self.a.arm and pend["trades"]:
                # ARMED: "entered" is what IBKR FILLED, never what the bar looked like. The
                # 2026-09-02 ALMS double position came from popping the pending on the bar's
                # say-so while the buy-stop still rested (IBKR triggers on quotes, not prints):
                # a ghost bracket, then a second bracket armed on the next rim.
                filled, qty = self._entry_filled(pend)
                if filled >= qty:                      # the whole entry is ours: the OCA exits own it now
                    self._settle_entry(sym, tf, pend, st, B, filled)
                    return
                if bar_entered or filled > 0:
                    pend["grace"] += 1                 # one bar of grace: a fill lags the bar by seconds
                    if pend["grace"] <= 1:
                        return
                    self._cancel_parent(pend, "grace over — a remainder never rests as a ghost")
                    if filled > 0:
                        self._say(f"  ⚠️ {B.ts[-1]:%m-%d %H:%M}  {sym} {tf} PARTIAL entry {filled}/{qty} sh — "
                                  f"remainder cancelled, the exits cover the filled shares")
                        self._settle_entry(sym, tf, pend, st, B, filled)
                    else:
                        self.pending.pop(sym)
                        self._missed_fill(sym, tf, pend, B)
                    return
            if st is None or st["state"] == "dead":
                self.cancel_pending(sym, "handle invalidated (too deep / too old)", B.ts[-1])
            elif bar_entered:
                self.entered_at[(sym, tf)] = st["entry_bar"]
                est = max(pend["trigger"], B.o[-1])    # gap over the open, else first touch
                if not self.a.arm:
                    self._say(f"  💥 {B.ts[-1]:%m-%d %H:%M}  {sym} {tf} WOULD FILL entry ≈ ${est:.2f} "
                              f"(trigger ${pend['trigger']:.2f}, bar open ${B.o[-1]:.2f})   [shadow]")
                    self.paper.append(dict(sym=sym, tf=tf, entry=est, trigger=pend["trigger"],
                                           stop=pend["stop"], target=pend["target"], ts=B.ts[-1],
                                           hi=est))                     # peak price seen (MFE tracking)
                self.pending.pop(sym)                  # shadow: the simulated exits own it from here
            else:
                self.update_pending(sym, st, B)
        else:
            # POST-EOD ARMING GUARD (audit find 2026-08-24, fixed 2026-09-02). Bars >= 15:49
            # never reach reconcile, BUT the 15:49 1-min bar still flushes the aggregators,
            # whose final 2/5-min buckets are stamped 15:45-15:48 and land HERE *after* the
            # flatten cleared the book. Arming off them would leave a fresh DAY bracket
            # resting 15:49-16:00 with nothing left to flatten it -> a fill there survives
            # OVERNIGHT. So live mode never arms again once the day's flatten has fired.
            # Replay stays exempt on purpose: it walks symbols SEQUENTIALLY (a later
            # symbol's whole day arrives after the first symbol's 15:49), which is exactly
            # the naive eod_done gate that broke replay in July.
            if self.eod_done and not self.a.replay:
                return
            for ri, st in sorted(scan.items()):        # arm the oldest armable forming setup
                if st["state"] == "forming" and n >= st["earliest"] and st["stop"] < float("inf"):
                    self.arm_pending(sym, tf, ri, st, B)
                    break

    def _settle_entry(self, sym, tf, pend, st, B, filled):
        """IBKR filled (part of) our entry: the bracket's OCA exits own the position from here."""
        bar_entered = st is not None and st["state"] == "entered"
        self.entered_at[(sym, tf)] = st["entry_bar"] if bar_entered else len(B) - 1
        self.pending.pop(sym, None)
        if not bar_entered:
            self._say(f"  💥 {B.ts[-1]:%m-%d %H:%M}  {sym} {tf} entry FILLED at IBKR ({filled} sh) before the "
                      f"bar showed a breakout (quote-triggered) — exits resting")

    def _missed_fill(self, sym, tf, pend, B):
        """The bar crossed the trigger, IBKR never filled us (stops trigger on quotes, not on a
        print). Logged + recorded as a cost-model datapoint — and never left resting."""
        hi = max(B.h[-2:]) if len(B) >= 2 else B.h[-1]
        self._say(f"  ❌ MISSED FILL {B.ts[-1]:%m-%d %H:%M}  {sym} {tf} bar high ${hi:.2f} crossed trigger "
                  f"${pend['trigger']:.2f} but IBKR filled 0 of {pend['qty']} sh — bracket cancelled")
        try:
            new = not os.path.exists(FILLS_CSV)
            with open(FILLS_CSV, "a") as f:
                if new:
                    f.write("time,symbol,ref,leg,action,shares,level,fill,slip_cents\n")
                f.write(f"{B.ts[-1]},{sym},{pend['ref']},MISSED,BUY,0,{pend['trigger']:.2f},{hi:.2f},\n")
        except OSError:
            pass

    # ---- safety -------------------------------------------------------------
    def _child_live(self, t) -> bool:
        """An exit child protects something only once its parent has filled; before that it is
        part of a RESTING bracket (IBKR shows it PreSubmitted) and must be neither counted
        as protection nor cancelled as an orphan."""
        pid = int(getattr(t.order, "parentId", 0) or 0)
        if not pid:
            return True
        par = self._parent_trades.get(pid)
        return par is None or int(getattr(par.orderStatus, "filled", 0) or 0) > 0

    def guard_brackets(self):
        """Runs every 30s on the main thread (never inside a stream callback). The invariant
        the 2026-09-02 ALMS trade broke: every share on OUR book has a WORKING stop, no exit
        order is larger than the book (an oversized fill flips us short), and a flat book has
        no exit orders left. OWN BOOK ONLY: ib.positions() also carries the tight-flag robot's
        shares (and, after a restart, a previous run's) — sizing stops off the account view
        would sell shares that are not ours. Only THIS session's refs are touched, and only
        exit children whose parent has filled count (the rest belong to resting brackets).
        Repairs are fresh orders placed INSIDE the ref's OCA group (10326: no revisions)."""
        if not self.a.arm or self.a.replay:
            return
        try:
            working = [t for t in self.ib.openTrades()
                       if t.orderStatus.status in ("PreSubmitted", "Submitted", "PendingSubmit")]
            acct = {p.contract.symbol: int(p.position) for p in self.ib.positions()
                    if p.contract.symbol in self.contracts}
        except Exception as ex:
            self._say(f"  ⚠️ guard: could not read positions/orders ({ex})")
            return
        own, level, ref_of = defaultdict(int), {}, {}
        for ref, p in self.open_real.items():
            if p["qty"] > 0:
                own[p["sym"]] += p["qty"]
                if p["sym"] not in level or p["stop"] > level[p["sym"]]:   # the tightest stop protects all
                    level[p["sym"]], ref_of[p["sym"]] = p["stop"], ref
        mine = [t for t in working if t.order.orderRef in self.brackets and t.order.action == "SELL"
                and t.order.orderType in ("STP", "LMT") and self._child_live(t)]
        for sym in sorted(set(own) | {t.contract.symbol for t in mine}):
            q = own.get(sym, 0)
            sells = [t for t in mine if t.contract.symbol == sym]
            if q <= 0:
                for t in sells:
                    self._cancel_order(t.order)
                if sells:
                    self._say(f"  🧹 guard: {sym} book is flat — cancelled {len(sells)} orphan exit order(s)")
                continue
            stops = [t for t in sells if t.order.orderType == "STP"]
            tps = [t for t in sells if t.order.orderType == "LMT"]
            remaining = lambda t: int(t.order.totalQuantity - (t.orderStatus.filled or 0))
            stop_qty, tp_qty = sum(map(remaining, stops)), sum(map(remaining, tps))
            ref = ref_of[sym]
            if stop_qty != q:
                if stop_qty > q:                       # oversized stop: a fill would flip us short
                    for t in stops:
                        self._cancel_order(t.order)
                    stop_qty = 0
                miss = q - stop_qty
                o = self.Order(action="SELL", orderType="STP", totalQuantity=miss, auxPrice=level[sym],
                               tif="DAY", orderRef=ref, ocaGroup=ref, ocaType=2)
                tr = self.ib.placeOrder(self.contracts[sym], o)
                tr.fillEvent += self._fill_logger("SL")
                self._say(f"  🚨 guard: {sym} book {q} sh had {stop_qty} sh of working stop — placed a stop "
                          f"for {miss} @ ${level[sym]:.2f} inside {ref}'s OCA group")
            if tp_qty > q:
                for t in tps:
                    self._cancel_order(t.order)
                self._say(f"  🚨 guard: {sym} take-profit size {tp_qty} > book {q} — cancelled "
                          f"(a fill would have flipped us short); stop stays")
        for sym, aq in sorted(acct.items()):
            if aq != own.get(sym, 0) and (sym, aq) not in self._acct_noted:
                self._acct_noted.add((sym, aq))
                self._say(f"  ℹ️ {sym}: account shows {aq:+d} sh, my book {own.get(sym, 0)} — the difference "
                          f"is another robot's or a previous run's; not mine to touch")

    def flatten(self, reason):
        """Cancel MY working orders and close MY book — nothing else. Until 2026-09-02 this
        was reqGlobalCancel + 'close every account position': it also killed the tight-flag
        robot's orders and sold ITS shares, and a flatten storm then oversold MU into a short.
        Own-book only now: one market close per ref (attributable in the executions), routed
        SMART, confirmed later by verify_flatten(). Account positions in our symbols that the
        book does not explain are shouted, never touched."""
        import time as _time
        if self._last_flatten and _time.time() - self._last_flatten < 15:
            self._say(f"  ↩️ flatten ({reason}) skipped — one is already in flight (closes fill in ms; "
                      f"ib.positions() lags, and re-placing sells oversold us into a SHORT on 2026-09-02)")
            return
        self._last_flatten = _time.time()
        n_cancel = 0
        try:
            for t in self.ib.openTrades():
                if t.order.orderRef in self.brackets and t.orderStatus.status not in DONE_STATES:
                    n_cancel += self._cancel_order(t.order)
        except Exception as ex:
            self._say(f"  ⚠️ could not list my open orders to cancel them: {ex}")
        for sym in list(self.pending):                 # a pending whose parent IBKR has not echoed yet
            pend = self.pending.pop(sym)
            if pend["trades"]:
                self._cancel_parent(pend, reason)
        now = self._last_t or datetime.now(ET).replace(tzinfo=None)
        if not self.a.arm and self.paper:              # close shadow positions at their last known price
            for p in self.paper:
                B = self.store.get((p["sym"], p["tf"]))
                px = B.c[-1] if B and len(B) else p["trigger"]
                risk = p["trigger"] - p["stop"]
                r = (px - p["entry"]) / risk if risk > 0 else 0.0
                self._say(f"  🕓 WOULD CLOSE ({reason}) {p['sym']} {p['tf']} @ ${px:.2f}  "
                          f"({r:+.2f}R vs entry ${p['entry']:.2f})   [shadow]")
                mfe = (p.get("hi", p["entry"]) - p["entry"]) / risk if risk > 0 else 0.0
                self.closed.append(dict(ts=now, sym=p["sym"], tf=p["tf"], kind="EOD", exit=px, r=r,
                                        entry=p["entry"], trigger=p["trigger"], stop=p["stop"],
                                        entry_ts=p.get("ts"), mfe=mfe))
            self.paper = []
        closes = []
        if self.a.arm:
            # AUDIT FIND 2026-09-01: ib.positions() hands back the LISTING exchange (e.g.
            # 'NASDAQ'), which is not an order route — a close placed on that contract is
            # rejected silently and the position survives overnight with its stop already
            # cancelled. Closes go on OUR qualified SMART contract, one per ref, and are
            # confirmed by verify_flatten(); the EOD fills zero the book like any other exit.
            for ref, p in list(self.open_real.items()):
                if p["qty"] <= 0:
                    continue
                inflight = self._inflight.get(ref)
                if inflight is not None and inflight.orderStatus.status not in DONE_STATES:
                    self._say(f"  ↩️ {p['sym']}: a close is already working ({inflight.orderStatus.status}) "
                              f"— not placing another")
                    continue
                c = self.contracts.get(p["sym"])
                if c is None:
                    from ib_async import Stock
                    c = Stock(p["sym"], "SMART", "USD")
                    try:
                        self.ib.qualifyContracts(c)
                    except Exception:
                        pass
                o = self.MarketOrder("SELL", p["qty"])
                o.orderRef = ref
                tr = self.ib.placeOrder(c, o)
                tr.fillEvent += self._fill_logger("EOD")
                self._inflight[ref] = tr
                closes.append((p["sym"], p["qty"], tr))
            own = defaultdict(int)
            for p in self.open_real.values():
                own[p["sym"]] += p["qty"]
            try:
                for pos in self.ib.positions():
                    sym = pos.contract.symbol
                    if pos.position != 0 and sym in self.contracts and int(pos.position) != own.get(sym, 0):
                        self._say(f"  🚨 {sym}: account holds {pos.position:+.0f} sh, my book {own.get(sym, 0)} — "
                                  f"NOT mine (another robot / a previous run), not touching it. "
                                  f"If it is yours, CLOSE IT MANUALLY")
            except Exception as ex:
                self._say(f"  ⚠️ could not read account positions: {ex}")
        n = len(closes)
        self._say(f"  ⛔ FLATTEN ({reason}) — cancelled {n_cancel} of my order(s), closing {n} position(s) "
                  f"({sum(q for _, q, _ in closes)} sh) — own book only")
        self._closes = closes
        if closes and not self._defer(3, self.verify_flatten):
            pass                                       # no event loop running (Ctrl-C path): main sleeps, then verifies

    def _defer(self, seconds: float, fn) -> bool:
        """Run fn after `seconds` WITHOUT blocking. Inside the IBKR event loop (every stream
        callback) a blocking ib.sleep() raises 'event loop is already running' — and eventkit
        swallows it silently (review find 2026-09-02: the flatten confirmation never ran and
        eod_done was never set). So: schedule on the running loop. Returns False when no loop
        is running (the Ctrl-C / replay path), in which case the caller sleeps and calls fn."""
        import asyncio
        try:
            asyncio.get_running_loop().call_later(seconds, fn)
            return True
        except RuntimeError:
            return False

    def verify_flatten(self):
        """A flatten is only real once IBKR confirms it. Idempotent."""
        closes, self._closes = getattr(self, "_closes", []), []
        bad = []
        for sym, qty, tr in closes:
            st = tr.orderStatus.status
            why = f" — {tr.log[-1].message}" if st != "Filled" and tr.log else ""
            self._say(f"  {'✅' if st == 'Filled' else '🚨'} close {sym} {qty:+.0f} sh: {st}{why}")
            if st != "Filled":
                bad.append(f"{sym} {qty:+.0f}")
        for ref, p in self.open_real.items():          # on the book with no close working at all
            if p["qty"] > 0 and self._inflight.get(ref) is None:
                bad.append(f"{p['sym']} {p['qty']:+d} (no close placed)")
        if bad:
            self._say("  🚨 STILL OPEN after flatten — CLOSE MANUALLY NOW: " + ", ".join(bad))

    # ---- bar pipeline ----------------------------------------------------------
    def on_closed_bar(self, sym, tf, t, o, h, l, c, v) -> bool:
        """One CLOSED bar of any timeframe -> append, run the pending lifecycle + the referee
        detector for that timeframe's series. Returns True if the bar was newly appended."""
        if self._report and self.cur_day is not None and t.date() < self.cur_day:
            return False                               # stale re-delivery of an OLDER session (farm
                                                       # reconnects replay old bars) -> never re-walk live
        key = (sym, tf)
        B = self.store.get(key)
        if B is None or B.date != t.date():
            B = Bars(symbol=sym, date=t.date(), timeframe=tf, ts=[], o=[], h=[], l=[], c=[], v=[])
            self.store[key] = B
            self.fired[key] = set()
            self._memo[key] = {}                       # a new day: nothing is final yet
        if B.ts and t <= B.ts[-1]:
            return False                               # already ingested (reconnect replays)
        B.ts.append(t); B.o.append(o); B.h.append(h); B.l.append(l); B.c.append(c); B.v.append(v)
        if tf == self.base_tf and t.date() != self.cur_day:   # new session -> re-open the trading day
            self.cur_day = t.date()
            self.eod_done = False
        if (self._report and tf == self.base_tf and len(B) == 1 and sym in self.unscreened
                and self.universe_hook is not None):
            self.unscreened.discard(sym)               # started pre-open: the $15 rule waits for today's open
            try:
                self.universe_hook(sym, o)
            except Exception as ex:
                self._say(f"  ⚠️ universe re-check failed for {sym}: {ex}")
            if sym in self.blocked:
                return True
        if self._report and tf == self.base_tf:        # proof-of-life: silence must never be ambiguous
            self._last_t = t
            if not self.live_started:
                self.live_started = True
                self._say(f"  ▶️ {t:%m-%d %H:%M} first LIVE bar ({sym} ${c:.2f}) — stream confirmed, watching for setups…")
            self._live1 += 1
            if self.last_beat is None or (t - self.last_beat).total_seconds() >= 900:
                self.last_beat = t
                self._say(f"  ❤️ {t:%m-%d %H:%M} alive — {self._live1} live 1min bars, "
                          f"{len(self.pending)} pending setup(s)")
            self._draw()                               # keep last-price / uP&L fresh (throttled)
        if self._report and not self.a.arm and self.paper:
            # shadow EXIT narration — mirrors the backtest labeler: checked from the bar AFTER
            # entry, on the position's own timeframe, stop before target when both touch.
            keep = []
            for p in self.paper:
                if p["sym"] != sym or p["tf"] != tf:
                    keep.append(p)
                    continue
                risk = p["trigger"] - p["stop"]
                p["hi"] = max(p.get("hi", p["entry"]), h)   # MFE: peak BEFORE exit check (labeler order)
                mfe = (p["hi"] - p["entry"]) / risk if risk > 0 else 0.0
                if l <= p["stop"]:
                    r = (p["stop"] - p["entry"]) / risk if risk > 0 else 0.0
                    self._say(f"  🩸 {t:%m-%d %H:%M}  {sym} {tf} WOULD STOP OUT @ ${p['stop']:.2f}  "
                              f"({r:+.2f}R vs entry ${p['entry']:.2f}, peaked +{mfe:.2f}R)   [shadow]")
                    self.closed.append(dict(ts=t, sym=sym, tf=tf, kind="STOP", exit=p["stop"], r=r,
                                            entry=p["entry"], trigger=p["trigger"], stop=p["stop"],
                                            entry_ts=p.get("ts"), mfe=mfe))
                elif h >= p["target"]:
                    r = (p["target"] - p["entry"]) / risk if risk > 0 else 0.0
                    self._say(f"  🎯 {t:%m-%d %H:%M}  {sym} {tf} WOULD TAKE PROFIT @ ${p['target']:.2f}  "
                              f"({r:+.2f}R vs entry ${p['entry']:.2f}, peaked +{mfe:.2f}R)   [shadow]")
                    self.closed.append(dict(ts=t, sym=sym, tf=tf, kind="TP", exit=p["target"], r=r,
                                            entry=p["entry"], trigger=p["trigger"], stop=p["stop"],
                                            entry_ts=p.get("ts"), mfe=mfe))
                else:
                    keep.append(p)
            self.paper = keep
        if t.time() >= EOD:                            # end of day
            if self._report and not self.eod_done:     # global flatten fires ONCE (orders, positions)
                self.eod_done = True                   # set FIRST: an exception inside flatten must never re-arm it
                self.flatten("EOD 15:49")
            elif self._report and not self.a.arm:
                # replay walks symbols sequentially: later symbols reach their own 15:49 AFTER the
                # global flatten already fired -> close THIS symbol's shadow day properly.
                if sym in self.pending:
                    self.cancel_pending(sym, "EOD — unfilled", t)
                keep = []
                for p in self.paper:
                    if p["sym"] != sym:
                        keep.append(p)
                        continue
                    B2 = self.store.get((p["sym"], p["tf"]))
                    px = B2.c[-1] if B2 and len(B2) else p["trigger"]
                    risk = p["trigger"] - p["stop"]
                    r = (px - p["entry"]) / risk if risk > 0 else 0.0
                    self._say(f"  🕓 WOULD CLOSE (EOD 15:49) {p['sym']} {p['tf']} @ ${px:.2f}  "
                              f"({r:+.2f}R vs entry ${p['entry']:.2f})   [shadow]")
                    mfe = (p.get("hi", p["entry"]) - p["entry"]) / risk if risk > 0 else 0.0
                    self.closed.append(dict(ts=t, sym=p["sym"], tf=p["tf"], kind="EOD", exit=px, r=r,
                                            entry=p["entry"], trigger=p["trigger"], stop=p["stop"],
                                            entry_ts=p.get("ts"), mfe=mfe))
                self.paper = keep
            return True
        if self._report:
            self.reconcile(sym, tf, B)
            # the FROZEN detector stays the referee: its entry events cross-check the pre-armer.
            # SPEED (2026-09-02): only while live (a full detect() per bar was 60% of the replay
            # and 100% of a slow seed — during seeding and replay it runs ONCE per symbol-day,
            # see referee_once()).
            if not self.a.replay:
                self.referee_once(sym, tf, t)
        return True

    def referee_once(self, sym, tf, t=None):
        """Cross-check every detector entry against what the pre-armer actually did."""
        key = (sym, tf)
        B = self.store.get(key)
        if not B or not len(B):
            return
        for e in self.det.detect(B, sym, B.date, signals_only=True):
            if e.breakout_idx in self.fired[key]:
                continue
            self.fired[key].add(e.breakout_idx)
            tag = ("pre-armed ✓" if self.entered_at.get(key) == e.breakout_idx
                   else "NOT pre-armed (occupied / cap / seeded mid-handle)")
            self._say(f"  📋 {B.ts[e.breakout_idx]:%m-%d %H:%M}  detector confirms entry {sym} {tf} "
                      f"${e.entry_price:.2f} — {tag}")

    def feed_1min(self, sym, t, o, h, l, c, v):
        """ONE closed BASE bar (15s in the program / 1min legacy) from ANY source ->
        the base pipeline, then (minute base only) the local k-min aggregators.
        Duplicates are dropped so sources can safely overlap."""
        if not (dtime(9, 30) <= t.time() <= dtime(16, 0)) or sym in self.blocked:
            return
        if not self.on_closed_bar(sym, self.base_tf, t, o, h, l, c, v):
            return                                     # duplicate -> don't double-feed the aggregators
        for k in self.agg_ks:
            agg = self.aggs.get((sym, k))
            if agg is None:
                agg = self.aggs[(sym, k)] = MinuteAggregator(
                    k, (lambda s_, k_: lambda ts, o_, h_, l_, c_, v_:
                        self.on_closed_bar(s_, f"{k_}min", ts, o_, h_, l_, c_, v_))(sym, k))
            agg.add(t, o, h, l, c, v)

    def ingest(self, sym, bl, report):
        """Feed IBKR's 1-min historical bar list (closed bars only — the forming last is skipped)."""
        self._report = report
        for b in bl[:-1]:
            t = b.date.astimezone(ET).replace(tzinfo=None) if hasattr(b.date, "astimezone") else b.date
            self.feed_1min(sym, t, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="?", default=None,
                    help="comma-separated tickers. If omitted, reads --watchlist; if that is "
                         "missing/empty, falls back to the built-in DEFAULT_SYMS.")
    ap.add_argument("--watchlist", default="auto",
                    help="ticker file, or 'auto' (default) = newest ~/Downloads/*DayTrade*.txt, "
                         "else data/watchlist.txt. Handles raw TradingView exports verbatim.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002, help="IB Gateway paper 4002 | TWS paper 7497")
    ap.add_argument("--client-id", type=int, default=8)
    ap.add_argument("--arm", action="store_true", help="actually PLACE paper orders (default: shadow, log only)")
    ap.add_argument("--risk", type=float, default=0.01, help="risk per trade as a fraction of equity")
    ap.add_argument("--base", type=float, default=0.0,
                    help="virtual starting equity for sizing, e.g. 1000 = trade the $1k plan on the "
                         "$1M paper account (compounds with this session's P&L). 0 = real account equity. "
                         "Cleaner long-term fix: reset the paper balance to $1,000 in Client Portal.")
    ap.add_argument("--tp", type=float, default=6.0, help="take-profit in R multiples (frozen eval: 6R best net)")
    ap.add_argument("--minstop", type=float, default=0.25,
                    help="minimum stop distance as %% of price (the research pile's floor — filters "
                         "sub-noise tiny-stop setups). 0 = off. User-approved 2026-07-16.")
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--tfs", default=DEFAULT_TFS,
                    help=f"timeframes to trade (default {DEFAULT_TFS} — the backtested trio; "
                         f"others allowed but UNTESTED)")
    ap.add_argument("--delayed", action="store_true", help="free delayed data (plumbing test; real eval needs real-time)")
    ap.add_argument("--poll", action="store_true",
                    help="fetch bars by re-requesting history on a timer instead of streaming — "
                         "a fallback for REAL-TIME subscriptions (useless on delayed: IBKR's delayed "
                         "historical serves completed sessions only)")
    ap.add_argument("--day", default=None,
                    help="replay only: the session date (YYYY-MM-DD). When the day's 15s bars are "
                         "already in cache/ibkr15s they are read from disk — no IBKR pull.")
    ap.add_argument("--replay", action="store_true",
                    help="walk the most recent COMPLETED session through the full live pipeline with "
                         "all lifecycle prints (run after ~16:20 ET to rehearse today), then exit")
    ap.add_argument("--i-understand-live", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.port in LIVE_PORTS and not a.i_understand_live:
        sys.exit(f"✗ port {a.port} is a LIVE trading port — this robot is PAPER-only. Refusing.")
    if a.replay and a.arm:
        sys.exit("✗ --replay with --arm makes no sense (orders on already-finished bars). Replay is shadow-only.")
    syms, src = read_watchlist(a.symbols, a.watchlist)
    tfs = [t.strip() for t in a.tfs.split(",")]
    if tfs == ["15s"]:
        # THE PROGRAM: base stream = native IBKR 15-sec bars (probe 2026-08-31: history
        # serves >=200 days of full 1,560-bar sessions, keepUpToDate attaches).
        base_tf, bar_size, seed_dur, agg_ks = "15s", "15 secs", "2 D", []
        if a.delayed:
            sys.exit("✗ 15s needs the real-time subscription — the delayed tier cannot serve it.")
    elif "15s" in tfs:
        sys.exit("✗ 15s runs alone (no cross-aggregation from a 15s base yet).")
    else:
        if "1min" not in tfs:
            sys.exit("✗ --tfs must include 1min (it is the base stream the others are built from).")
        try:
            agg_ks = sorted({int(t[:-3]) for t in tfs if t != "1min"})
            assert all(t.endswith("min") and int(t[:-3]) > 0 for t in tfs)
        except (ValueError, AssertionError):
            sys.exit(f"✗ bad --tfs '{a.tfs}' — use e.g. 1min,2min,5min or just 15s")
        base_tf, bar_size, seed_dur = "1min", "1 min", "5 D"
    for t in tfs:
        if t not in BACKTESTED_TFS:
            print(f"⚠️ {t} was NEVER BACKTESTED — no evidence it has an edge. Backfill + label a {t} "
                  f"pile first; until then its trades are guesses.")

    try:
        from ib_async import IB, Stock, Order, MarketOrder
    except ImportError:
        sys.exit("✗ ib_async not installed.  ->  pip install ib_async")

    ib = IB()
    try:
        ib.connect(a.host, a.port, clientId=a.client_id, timeout=10)
    except Exception as e:
        sys.exit(f"✗ can't reach IB Gateway/TWS at {a.host}:{a.port} — running? API enabled? port right?  ({e})")
    ib.reqMarketDataType(3 if a.delayed else 1)
    ib.sleep(1)

    bot = Trader(ib, Order, MarketOrder, a, agg_ks, base_tf=base_tf)

    _CHATTER = {2104, 2106, 2107, 2108, 2119, 2158, 1102}     # farm/data-status notices

    def on_error(reqId, code, msg, contract=None):
        """AUDIT FIND 2026-09-01: order rejections and disconnects were only ever printed
        to the terminal — invisible in the log, invisible on the board. Now they are
        part of the day's record."""
        if code in _CHATTER:
            return
        sym = f" {contract.symbol}" if contract is not None and getattr(contract, "symbol", "") else ""
        bot._say(f"  ⚠️ IBKR error {code}{sym} (req {reqId}): {msg}")
    ib.errorEvent += on_error
    mode = "🔴 ARMED — placing PAPER orders" if a.arm else "🟢 SHADOW — logging only, no orders"
    print(f"ORDER ROBOT — IBKR paper — {mode}")
    print(f"  PRE-ARM entries: brackets REST during the handle (backtest-faithful first-touch fills)")
    eq_note = f"  (virtual --base on the real account)" if a.base > 0 else ""
    print(f"  account {ib.managedAccounts()}  sizing equity ${bot.equity():,.0f}{eq_note}")
    print(f"  data: {'delayed' if a.delayed else 'real-time'} | tfs {','.join(tfs)} (one stream, local agg) | "
          f"risk {a.risk*100:.1f}% | tp {a.tp:g}R | cap {a.max_positions} | fills -> {FILLS_CSV}")
    print(f"  watchlist ({len(syms)}) from {src}")
    print(f"  watching {', '.join(syms)}")
    archive_watchlist(resolve_watchlist(a.watchlist))
    print()

    def on_update(bars, has_new_bar):
        if has_new_bar:
            bot.ingest(bars.contract.symbol, bars, report=True)

    stream = not a.poll and not a.replay
    seeds = []                                         # (symbol, BarDataList) — so tick mode can detach them
    if a.replay:
        print("  REPLAY MODE — walking the most recent COMPLETED session. ⚠️ run BEFORE ~16:15 ET and "
              "that is the PREVIOUS trading day, not today. All times below belong to that session.\n")
    skipped = []
    universe_rows: list[dict] = []
    try:                                               # source tags from the export (###CNBC …)
        from record_day import parse_sources
        _exp = resolve_watchlist(a.watchlist) if not a.symbols else None
        SOURCES = parse_sources(_exp) if _exp else {}
    except Exception:
        SOURCES = {}

    SEEDS: dict = {}                                   # symbol -> its live BarDataList (to detach on a drop)
    DETAILS: dict = {}                                 # symbol -> IBKR ContractDetails (stockType/industry)

    def drop_symbol(s: str, why: str, say=print, bl=None) -> None:
        """Take a symbol out of the run — at the seed, or on today's first bar."""
        if s not in skipped:
            skipped.append(s)
        bot.blocked.add(s)
        bot.unscreened.discard(s)
        bot.contracts.pop(s, None)
        for k_ in [k_ for k_ in bot.store if k_[0] == s]:
            bot.store.pop(k_, None)
        bl = bl if bl is not None else SEEDS.pop(s, None)
        if bl is not None:
            try:
                bl.updateEvent -= on_update
            except Exception:
                pass
            try:
                ib.cancelHistoricalData(bl)
            except Exception:
                pass
        say(f"  🚫 {s}: DROPPED — {why}")

    def note_universe(s: str, price, why: str) -> dict:
        """The day's verdict row for this symbol (a re-check on today's open replaces it)."""
        srcs = SOURCES.get(s, [])
        row = {"day": f"{datetime.now(ET):%Y-%m-%d}", "symbol": s, "sources": "+".join(srcs),
               "n_sources": len(srcs), "open": f"{price:.2f}" if price is not None else "",
               "qualified": "no" if why else "yes", "reason": why}
        universe_rows[:] = [r for r in universe_rows if r["symbol"] != s] + [row]
        return row

    def universe_hook(sym: str, open_px: float) -> None:
        """Pre-open start: the seed had no bar of today, so the $15 rule could not run. It
        runs HERE, on today's first bar — before any cup can possibly form (>= 19 bars)."""
        why = universe_verdict(open_px, DETAILS.get(sym))
        row = note_universe(sym, open_px, why)
        record_universe(row["day"], [row])
        if why:
            drop_symbol(sym, why, bot._say)
        else:
            bot._say(f"  ✅ {sym}: opened ${open_px:.2f} — passes the universe screen")
    bot.universe_hook = universe_hook

    def setup_symbol(s: str, say=print) -> bool:
        """Bring ONE symbol online: qualify, seed history, attach the stream. Used at
        startup and by the mid-session hot-add (same path, so a 09:55 addition behaves
        exactly like a late start: the seed shows it the whole morning, arming begins
        on the next live bar)."""
        c = Stock(s, "SMART", "USD")
        try:
            # A raw TradingView export can contain non-equities (TVC:VIX, TVC:USOIL, futures,
            # crypto). Those are not US stocks, so skip them instead of killing the whole run.
            # NOTE: qualifyContracts returns a truthy list even when IBKR rejects the symbol —
            # the real test is whether a conId came back.
            ib.qualifyContracts(c)
            if not getattr(c, "conId", 0):
                raise ValueError("unknown contract — not a US stock at IBKR")
        except Exception as ex:
            skipped.append(s)
            say(f"  {s}: SKIPPED — {ex if str(ex) else type(ex).__name__} "
                f"(not a US stock at IBKR? remove it from the watchlist)")
            return False
        bot.contracts[s] = c
        # 5 D lookback, NOT 1 D: the window must ALWAYS contain >=1 trading session (holiday
        # weekends!). NOTE: on the free DELAYED tier this returns COMPLETED sessions only —
        # today's bars are invisible until the close; tick-mode below covers today.
        cached = cached_day_bars(s, a.day) if (a.replay and a.day and base_tf == "15s") else None
        if cached is not None:
            bl = cached                                # replay from disk: seconds, not minutes
        elif a.replay and a.day:                       # cache miss on a NAMED day: pull THAT day, never "latest"
            end = datetime.fromisoformat(a.day).replace(hour=23, minute=59, tzinfo=ET)
            bl = ib.reqHistoricalData(c, endDateTime=end, durationStr="1 D", barSizeSetting=bar_size,
                                      whatToShow="TRADES", useRTH=True, formatDate=2, keepUpToDate=False)
        else:
            bl = ib.reqHistoricalData(c, endDateTime="", durationStr=seed_dur, barSizeSetting=bar_size,
                                      whatToShow="TRADES", useRTH=True, formatDate=2, keepUpToDate=stream)
        live_bl = bl if (stream and cached is None) else None
        if not len(bl):                                # halted / delisted / never printed: nothing to trade
            drop_symbol(s, "no bars served (halted or not trading)", say, live_bl)
            return False
        feed = bl
        ref_day = datetime.now(ET).date()              # the day whose OPEN the $15 rule is judged on
        def _d(x):
            return x.date.astimezone(ET).date() if hasattr(x.date, "astimezone") else x.date.date()
        if a.replay:                                   # replay ONLY the most recent session in the window
            ref_day = max(_d(x) for x in bl)
            feed = [x for x in bl if _d(x) == ref_day]
        # ---- THE UNIVERSE SCREEN: price >= $15, common stock, not commodity (old rules) ----
        # Judged on the DAY's open, BEFORE the bars are walked (a replay walks the whole day
        # on ingest — screening afterwards left the dropped names' trades in the summary).
        # Before the open the price is unknown (yesterday's close is not the rule and dropped
        # a name for good) -> the $15 rule re-runs on today's first bar (universe_hook).
        first = next((x for x in feed if _d(x) == ref_day), None)
        price = float(first.open) if first is not None else None
        try:
            cds = ib.reqContractDetails(c)
            DETAILS[s] = cds[0] if cds else None
        except Exception:
            DETAILS[s] = None
        why = universe_verdict(price, DETAILS[s])
        if not a.replay:                               # replay screens but records nothing (history is
            note_universe(s, price, why)               # record_day.py's; today's rows are the live bot's)
        if why:
            drop_symbol(s, why, say, live_bl)
            return False
        if price is None and not a.replay:
            bot.unscreened.add(s)                      # pre-open start: judged on today's first bar
        was_live = bot._report
        bot.ingest(s, feed, report=a.replay)           # replay: walk that session with full lifecycle prints
        bot._report = was_live or a.replay             # a mid-session seed must not silence the live board
        if a.replay:
            for tf_ in tfs:
                bot.referee_once(s, tf_)               # once per symbol-day instead of once per bar
        seeded = " ".join(f"{len(bot.store.get((s, t), []))} {t}" for t in tfs)
        if live_bl is not None:
            bl.updateEvent += on_update
            seeds.append((s, bl))
            SEEDS[s] = bl
        say(f"  {s}: seeded {seeded} bars" + (" (from cache)" if cached is not None else "")
            + (" — $15 rule runs on today's open" if price is None and not a.replay else ""))
        return True

    for s in syms:
        setup_symbol(s)
    if universe_rows:
        record_universe(universe_rows[0]["day"], universe_rows)
        print(f"  📚 universe verdicts recorded -> data/universe_log.csv "
              f"({sum(1 for r in universe_rows if r['qualified'] == 'yes')} tradable, "
              f"{sum(1 for r in universe_rows if r['qualified'] == 'no')} dropped with reasons)")
    if skipped:
        syms = [s for s in syms if s not in skipped]
        print(f"\n  ⚠️ {len(skipped)} symbol(s) skipped: {', '.join(skipped)} — trading {len(syms)}")
    if not syms:
        sys.exit("✗ no tradable symbols left — check data/watchlist.txt")

    if a.replay:
        rlatest = max((B.ts[-1] for (s, t), B in bot.store.items() if t == base_tf and len(B)), default=None)
        rday = f"{rlatest:%A %Y-%m-%d}" if rlatest else "?"
        print(f"\n▶️ REPLAY complete — session replayed: {rday}. Every 🛡️/🔧/💥/🗑️/📋 above is what "
              f"live would have printed on that day. Compare charts against THAT date. No stream started.")
        if bot.closed:
            print(f"\n  ── TRADES SUMMARY ({rday}) ──")
            print(f"  {'closed':<8}{'sym':<8}{'tf':<6}{'exit':<6}{'@ price':>10}{'result':>9}")
            for c in bot.closed:
                print(f"  {c['ts']:%H:%M}   {c['sym']:<8}{c['tf']:<6}{c['kind']:<6}{c['exit']:>10.2f}{c['r']:>+8.2f}R")
            tot = sum(c["r"] for c in bot.closed)
            wins = sum(1 for c in bot.closed if c["r"] > 0)
            print(f"  {'':>8}{len(bot.closed)} trade(s), {wins} win(s), day total {tot:+.2f}R")
        else:
            print("\n  ── TRADES SUMMARY: no completed trades that day ──")
        # ---- permanent daily ledger (user request 2026-07-22): one row per trade, per variant ----
        import csv
        LEDGER = "data/replay_trades.csv"
        sess = f"{rlatest:%Y-%m-%d}" if rlatest else "unknown"
        variant = f"minstop={a.minstop:g}"
        cols = ["session", "variant", "symbol", "tf", "entry_time", "entry", "trigger", "stop",
                "stop_pct", "exit_time", "exit_kind", "exit", "R", "peak_R"]
        old_rows = []
        if os.path.exists(LEDGER):
            with open(LEDGER) as f:
                old_rows = [r for r in csv.DictReader(f)
                            if not (r.get("session") == sess and r.get("variant") == variant)]
        for c in bot.closed:                           # idempotent: same day+variant replaces itself
            trig, stop = c.get("trigger"), c.get("stop")
            old_rows.append({
                "session": sess, "variant": variant, "symbol": c["sym"], "tf": c["tf"],
                "entry_time": f"{c['entry_ts']:%H:%M}" if c.get("entry_ts") else "",
                "entry": f"{c['entry']:.2f}" if c.get("entry") is not None else "",
                "trigger": f"{trig:.2f}" if trig else "", "stop": f"{stop:.2f}" if stop else "",
                "stop_pct": f"{(trig - stop) / trig * 100:.2f}" if trig and stop else "",
                "exit_time": f"{c['ts']:%H:%M}", "exit_kind": c["kind"],
                "exit": f"{c['exit']:.2f}", "R": f"{c['r']:.2f}",
                "peak_R": f"{c['mfe']:.2f}" if c.get("mfe") is not None else ""})
        with open(LEDGER, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", restval="")
            w.writeheader()
            for r in sorted(old_rows, key=lambda x: (x["session"], x["variant"], x["exit_time"])):
                w.writerow(r)
        print(f"  📒 ledger: {len(bot.closed)} trade(s) recorded -> {LEDGER}  ({sess}, {variant})")
        ib.disconnect()
        return

    today = datetime.now(ET).date()
    latest = max((B.ts[-1] for (s, t), B in bot.store.items() if t == base_tf and len(B)), default=None)
    tickmode = a.delayed and (latest is None or latest.date() < today)
    if tickmode:
        # today is invisible to delayed HISTORICAL -> build today's 1min bars from delayed TICKS.
        # DETACH the historical stream entirely: it has nothing to add for today, and on farm
        # reconnects it RE-DELIVERS old sessions — whose 15:49 bar fires a spurious mid-day flatten.
        for _s, _bl in seeds:
            try:
                _bl.updateEvent -= on_update
                ib.cancelHistoricalData(_bl)
            except Exception:
                pass
        print("\n🔁 delayed historical serves only COMPLETED sessions (today is invisible) — "
              "switching to DELAYED-TICK bar building for today (historical stream detached). "
              "Bars stamped wall-clock − 15 min.")
        lag = timedelta(minutes=15)
        bot._report = True
        builders = {s: TickBarBuilder((lambda s_: lambda ts, o, h, l, c, v:
                                       bot.feed_1min(s_, ts, o, h, l, c, v))(s)) for s in syms}
        for s in syms:
            ib.reqMktData(bot.contracts[s], "", False, False)

        def on_ticks(tickers):
            now = datetime.now(ET).replace(tzinfo=None) - lag
            for tk in tickers:
                bld = builders.get(tk.contract.symbol)
                px = tk.last
                if bld and px is not None and px == px and px > 0:
                    bld.on_tick(now, px, tk.volume)
        ib.pendingTickersEvent += on_ticks
    elif latest is not None and latest.date() < today:
        print(f"\n⏳ seeded the prior session ({latest:%Y-%m-%d}); waiting for TODAY's bars. Leave it running.")

    poll_iv = max(30, len(syms) * 12)                  # IB pacing: <= 60 historical requests / 10 min
    label = f"running (POLLING every {poll_iv}s)…" if a.poll else "running…"
    print(f"\n{label}  (Ctrl-C to flatten everything + stop)\n")
    def hot_add():
        """Every 30s: if a NEWER export (saved today) names symbols we are not running,
        bring them online without touching anything already live. Additive only.
        Skipped when the symbols came from the command line (an explicit list is fixed).
        Review finds 2026-09-02: only during the session (09:30-15:45 — a seed after 15:49
        re-opens the day and fires a second flatten; a post-close export is TOMORROW's
        list), never in delayed tick mode (new names could not stream), and never fatal
        (an exception here on the main thread would skip the kill-switch flatten)."""
        if a.symbols or tickmode:
            return
        now = datetime.now(ET)
        if not (dtime(9, 30) <= now.time() < dtime(15, 45)):
            return
        try:
            path = resolve_watchlist(a.watchlist)
            new = watchlist_additions(set(bot.contracts) | set(skipped), path, now.date())
            if not new:
                return
            bot._say(f"  🧩 {now:%H:%M} watchlist grew — adding {', '.join(new)}")
            try:
                from record_day import parse_sources
                SOURCES.update(parse_sources(path))
            except Exception:
                pass
            for s_ in new:
                if setup_symbol(s_, say=bot._say):
                    syms.append(s_)
            fresh = [r for r in universe_rows if r["symbol"] in new]
            if fresh:
                record_universe(fresh[0]["day"], fresh)
            archive_watchlist(path)                    # the day's archive = the day's final list
        except Exception as ex:
            bot._say(f"  ⚠️ hot-add failed ({type(ex).__name__}: {ex}) — still trading the current list")

    try:
        if a.poll:
            while True:
                ib.sleep(max(30, len(syms) * 12))     # re-sized: hot_add can grow the list
                hot_add()
                bot.guard_brackets()
                for s in list(syms):
                    pl = ib.reqHistoricalData(bot.contracts[s], endDateTime="", durationStr="1 D",
                                              barSizeSetting=bar_size, whatToShow="TRADES",
                                              useRTH=True, formatDate=2, keepUpToDate=False)
                    bot.ingest(s, pl, report=True)
        else:
            while True:                                # ib.sleep keeps the event loop (streams) running
                ib.sleep(30)
                hot_add()
                bot.guard_brackets()
    except KeyboardInterrupt:
        print("\n⛔ kill-switch — flattening…")
        bot.flatten("kill-switch")
        ib.sleep(3)
        bot.verify_flatten()
    except Exception as ex:                            # never exit with positions open, silently
        print(f"\n💥 fatal: {type(ex).__name__}: {ex} — attempting to flatten…")
        try:
            bot.flatten("fatal error")
            ib.sleep(3)
            bot.verify_flatten()
        except Exception as ex2:
            print(f"   flatten impossible ({ex2}) — CHECK THE ACCOUNT MANUALLY")
        raise
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
