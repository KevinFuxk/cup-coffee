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
  UPDATE    each closed bar while forming: handle low drifts down -> modify stop leg, resize qty, move target
  CANCEL    handle invalidates (deeper than 20% of cup, or too old) -> cancel the pending bracket
  FILL      logged to data/paper_fills.csv with slippage vs the intended level  <- THE measurement
  FLATTEN   EOD 15:49 or Ctrl-C -> cancel everything + close all positions

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
DEFAULT_TFS = "1min,2min,5min"     # 1/2/5 = backtested pile; 3min = user's call, backfill pending
BACKTESTED_TFS = {"1min", "2min", "5min"}
EOD = dtime(15, 49)
LIVE_PORTS = {4001, 7496}
FILLS_CSV = "data/paper_fills.csv"
OPEN_MIN = 9 * 60 + 30                  # 09:30 in minutes — aggregation anchor


def resolve_watchlist(path: str) -> str | None:
    """Turn --watchlist into a real file. 'auto' (the default) picks the NEWEST
    *DayTrade*.txt in ~/Downloads — so a TradingView export named '8_21_2026 DayTrade.txt'
    is found without typing the date. Falls back to data/watchlist.txt. Pure local
    filesystem lookup: no network, nothing that can move under us."""
    import glob
    if path and path != "auto":
        return path if os.path.exists(path) else None
    # match case-INSENSITIVELY: TradingView names the export after the watchlist,
    # and "daytrade" vs "DayTrade" must not silently fall back to a stale list
    # (2026-08-26: a lowercase rename made the bot trade the 8/21 watchlist).
    cands = [c for c in glob.glob(os.path.expanduser("~/Downloads/*.txt"))
             if "daytrade" in os.path.basename(c).lower()]
    if cands:
        newest = max(cands, key=os.path.getmtime)
        return newest
    return "data/watchlist.txt" if os.path.exists("data/watchlist.txt") else None


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
        syms = clean(open(resolved).read())
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
    os.makedirs("data/watchlists", exist_ok=True)
    dst = f"data/watchlists/{datetime.now(ET):%Y-%m-%d}.txt"
    content = open(path).read()
    if os.path.exists(dst) and open(dst).read() == content:
        return
    with open(dst, "w") as f:
        f.write(content)
    print(f"  📚 watchlist archived -> {dst}")


def scan_setups(det: PatternDetector, b: Bars) -> dict[int, dict]:
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
    for li in range(1, n - 1):
        if not _is_peak(b, li):
            continue
        min_ri = 0
        while True:                                    # ROLLING RIM (user spec 2026-07-20): a pre-entry
            cup = det._find_cup(b, li, min_ri)         # bar above the rim dethrones it -> re-search for
            if cup is None:                            # the next peak-confirmed rim that re-passes gates
                break
            bottom_idx, ri = cup
            cup_low = b.l[bottom_idx]
            rim, lip = b.h[ri], b.h[li]
            depth_h = rim - cup_low
            if depth_h <= 0:
                break
            _d = min if det.rim_mode == "min" else max     # rim symmetry, same as _find_handle
            if abs(rim - lip) >= det.rim_recov * _d(lip - cup_low, rim - cup_low):
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
            if ri not in out:                          # first successful li wins (detector order)
                out[ri] = dict(state=state, trigger=trigger, stop=hl, earliest=earliest,
                               entry_bar=entry_bar, momentum=momentum)
            break
    return out


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
    def __init__(self, ib, Order, MarketOrder, args, agg_ks):
        self.ib, self.Order, self.MarketOrder, self.a = ib, Order, MarketOrder, args
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
                B = self.store.get((sym, "1min"))
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
        """Native IBKR bracket: STP parent + OCA (LMT take-profit / STP stop-loss) children."""
        c = self.contracts[sym]
        pid = self.ib.client.getReqId()
        ref = pend["ref"]
        parent = self.Order(orderId=pid, action="BUY", orderType="STP", totalQuantity=pend["qty"],
                            auxPrice=pend["trigger"], tif="DAY", transmit=False, orderRef=ref)
        tp = self.Order(orderId=self.ib.client.getReqId(), action="SELL", orderType="LMT",
                        totalQuantity=pend["qty"], lmtPrice=pend["target"], tif="DAY", parentId=pid,
                        transmit=False, orderRef=ref, ocaGroup=ref, ocaType=1)
        sl = self.Order(orderId=self.ib.client.getReqId(), action="SELL", orderType="STP",
                        totalQuantity=pend["qty"], auxPrice=pend["stop"], tif="DAY", parentId=pid,
                        transmit=True, orderRef=ref, ocaGroup=ref, ocaType=1)
        trades = {}
        for name, o in (("ENTRY", parent), ("TP", tp), ("SL", sl)):
            tr = self.ib.placeOrder(c, o)
            tr.fillEvent += self._fill_logger(name)
            trades[name] = tr
        self.brackets[ref] = pend                      # levels for fill bookkeeping (updates track pend)
        return trades

    def _fill_logger(self, kind):
        def on_fill(trade, fill):
            o = trade.order
            level = o.lmtPrice if o.orderType == "LMT" else o.auxPrice
            px = fill.execution.price
            slip = (px - level) if o.action == "BUY" else (level - px)   # + = worse than intended
            self._say(f"  💰 FILL {kind:5} {trade.contract.symbol} {fill.execution.shares:.0f}@${px:.2f} "
                      f"(level ${level:.2f}, slip {slip*100:+.1f}¢)")
            br = self.brackets.get(o.orderRef, {})
            now = self._last_t or datetime.now(ET).replace(tzinfo=None)
            if kind == "ENTRY":                        # position opened -> shows on the board
                self.open_real[o.orderRef] = dict(sym=trade.contract.symbol, tf=br.get("tf", "?"),
                                                  entry=px, stop=br.get("stop", level),
                                                  target=br.get("target", 0.0),
                                                  trigger=br.get("trigger", level))
            elif kind in ("TP", "SL"):                 # position closed -> board + day tally
                pos = self.open_real.pop(o.orderRef, None)
                if pos:
                    risk = pos["trigger"] - pos["stop"]
                    r = (px - pos["entry"]) / risk if risk > 0 else 0.0
                    self.closed.append(dict(ts=now, sym=pos["sym"], tf=pos["tf"],
                                            kind=kind, exit=px, r=r))
            new = not os.path.exists(FILLS_CSV)
            with open(FILLS_CSV, "a") as f:
                if new:
                    f.write("time,symbol,ref,leg,action,shares,level,fill,slip_cents\n")
                f.write(f"{fill.time},{trade.contract.symbol},{o.orderRef},{kind},{o.action},"
                        f"{fill.execution.shares:.0f},{level:.2f},{px:.2f},{slip*100:.1f}\n")
        return on_fill

    def modify_bracket(self, pend):
        try:
            for name, tr in pend["trades"].items():
                o = tr.order
                o.totalQuantity = pend["qty"]
                if name == "SL":
                    o.auxPrice = pend["stop"]
                if name == "TP":
                    o.lmtPrice = pend["target"]
                o.transmit = True
                self.ib.placeOrder(tr.contract, o)
        except Exception as ex:
            self._say(f"  ⚠️ modify failed for {pend['ref']}: {ex}")

    # ---- pending-setup lifecycle -------------------------------------------
    def arm_pending(self, sym, tf, ri, st, B):
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
        pend = dict(ri=ri, tf=tf, ref=ref, trigger=trigger, stop=stop, qty=qty, target=target,
                    trades=None, ts=B.ts[-1])
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
        risk = pend["trigger"] - new_stop
        eq = self.equity()
        qty = max(1, int((self.a.risk * eq) / risk)) if eq > 0 else pend["qty"]
        pend.update(stop=new_stop, qty=qty, target=round(pend["trigger"] + self.a.tp * risk, 2))
        self._say(f"  🔧 {B.ts[-1]:%m-%d %H:%M}  {sym} {pend['tf']} handle deepened -> stop ${new_stop:.2f}, "
                  f"{qty} sh, tp ${pend['target']:.2f}")
        if pend["trades"]:
            self.modify_bracket(pend)

    def cancel_pending(self, sym, reason, ts=None):
        pend = self.pending.pop(sym)
        if pend["trades"]:
            try:
                self.ib.cancelOrder(pend["trades"]["ENTRY"].order)   # children die with the parent
            except Exception as ex:
                self._say(f"  ⚠️ cancel failed for {pend['ref']}: {ex}")
        when = f"{ts:%m-%d %H:%M}  " if ts else ""
        self._say(f"  🗑️ CANCEL {when}{sym} {pend['tf']} pending bracket — {reason}")

    def reconcile(self, sym, tf, B):
        """Per closed bar of THIS timeframe: sync the symbol's one pending setup with the scanner."""
        pend = self.pending.get(sym)
        if pend and pend["tf"] != tf:
            return                                     # slot held by another timeframe — cross-tf dedup
        scan = scan_setups(self.det, B)
        n = len(B)
        if pend:
            st = scan.get(pend["ri"])
            if st is None or st["state"] == "dead":
                self.cancel_pending(sym, "handle invalidated (too deep / too old)", B.ts[-1])
            elif st["state"] == "entered":
                self.entered_at[(sym, tf)] = st["entry_bar"]
                est = max(pend["trigger"], B.o[-1])    # gap over the open, else first touch
                if not self.a.arm:
                    self._say(f"  💥 {B.ts[-1]:%m-%d %H:%M}  {sym} {tf} WOULD FILL entry ≈ ${est:.2f} "
                              f"(trigger ${pend['trigger']:.2f}, bar open ${B.o[-1]:.2f})   [shadow]")
                    self.paper.append(dict(sym=sym, tf=tf, entry=est, trigger=pend["trigger"],
                                           stop=pend["stop"], target=pend["target"], ts=B.ts[-1],
                                           hi=est))                     # peak price seen (MFE tracking)
                self.pending.pop(sym)                  # real mode: the OCA exits own it from here
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

    # ---- safety -------------------------------------------------------------
    def flatten(self, reason):
        self.ib.reqGlobalCancel()
        self.pending.clear()
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
        if self.a.arm and self.open_real:              # armed: tally EOD closes at last known price
            for ref, p in self.open_real.items():
                B = self.store.get((p["sym"], p["tf"]))
                px = B.c[-1] if B and len(B) else p["entry"]
                risk = p["trigger"] - p["stop"]
                r = (px - p["entry"]) / risk if risk > 0 else 0.0
                self.closed.append(dict(ts=now, sym=p["sym"], tf=p["tf"], kind="EOD", exit=px, r=r))
            self.open_real = {}
        n = 0
        for p in self.ib.positions():
            if p.position != 0:
                act = "SELL" if p.position > 0 else "BUY"
                self.ib.placeOrder(p.contract, self.MarketOrder(act, abs(p.position)))
                n += 1
        self._say(f"  ⛔ FLATTEN ({reason}) — cancelled all orders (incl. pendings), closing {n} position(s)")

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
        if B.ts and t <= B.ts[-1]:
            return False                               # already ingested (reconnect replays)
        B.ts.append(t); B.o.append(o); B.h.append(h); B.l.append(l); B.c.append(c); B.v.append(v)
        if tf == "1min" and t.date() != self.cur_day:  # new session -> re-open the trading day
            self.cur_day = t.date()
            self.eod_done = False
        if self._report and tf == "1min":              # proof-of-life: silence must never be ambiguous
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
                self.flatten("EOD 15:49")
                self.eod_done = True
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
        # the FROZEN detector stays the referee: its entry events cross-check the pre-armer
        for e in self.det.detect(B, sym, B.date, signals_only=True):
            if e.breakout_idx in self.fired[key]:
                continue
            self.fired[key].add(e.breakout_idx)
            if self._report:
                tag = ("pre-armed ✓" if self.entered_at.get(key) == e.breakout_idx
                       else "NOT pre-armed (occupied / cap / seeded mid-handle)")
                self._say(f"  📋 {t:%m-%d %H:%M}  detector confirms entry {sym} {tf} ${e.entry_price:.2f} — {tag}")
        return True

    def feed_1min(self, sym, t, o, h, l, c, v):
        """ONE closed 1-min bar from ANY source (historical seed, stream, or tick-built) ->
        the 1min pipeline, then the local 2/3/5min aggregators (their closed buckets run
        their own pipelines). Duplicates are dropped so sources can safely overlap."""
        if not (dtime(9, 30) <= t.time() <= dtime(16, 0)):
            return
        if not self.on_closed_bar(sym, "1min", t, o, h, l, c, v):
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
    if "1min" not in tfs:
        sys.exit("✗ --tfs must include 1min (it is the base stream the others are built from).")
    try:
        agg_ks = sorted({int(t[:-3]) for t in tfs if t != "1min"})
        assert all(t.endswith("min") and int(t[:-3]) > 0 for t in tfs)
    except (ValueError, AssertionError):
        sys.exit(f"✗ bad --tfs '{a.tfs}' — use e.g. 1min,2min,5min")
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

    bot = Trader(ib, Order, MarketOrder, a, agg_ks)
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
    for s in syms:
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
            print(f"  {s}: SKIPPED — {ex if str(ex) else type(ex).__name__} "
                  f"(not a US stock at IBKR? remove it from the watchlist)")
            continue
        bot.contracts[s] = c
        # 5 D lookback, NOT 1 D: the window must ALWAYS contain >=1 trading session (holiday
        # weekends!). NOTE: on the free DELAYED tier this returns COMPLETED sessions only —
        # today's bars are invisible until the close; tick-mode below covers today.
        bl = ib.reqHistoricalData(c, endDateTime="", durationStr="5 D", barSizeSetting="1 min",
                                  whatToShow="TRADES", useRTH=True, formatDate=2, keepUpToDate=stream)
        feed = bl
        if a.replay and len(bl):                       # replay ONLY the most recent session in the window
            def _d(x):
                return x.date.astimezone(ET).date() if hasattr(x.date, "astimezone") else x.date.date()
            last_day = max(_d(x) for x in bl)
            feed = [x for x in bl if _d(x) == last_day]
        bot.ingest(s, feed, report=a.replay)           # replay: walk that session with full lifecycle prints
        seeded = " ".join(f"{len(bot.store.get((s, t), []))} {t}" for t in tfs)
        if stream:
            bl.updateEvent += on_update
            seeds.append((s, bl))
        print(f"  {s}: seeded {seeded} bars")
    if skipped:
        syms = [s for s in syms if s not in skipped]
        print(f"\n  ⚠️ {len(skipped)} symbol(s) skipped: {', '.join(skipped)} — trading {len(syms)}")
    if not syms:
        sys.exit("✗ no tradable symbols left — check data/watchlist.txt")

    if a.replay:
        rlatest = max((B.ts[-1] for (s, t), B in bot.store.items() if t == "1min" and len(B)), default=None)
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
    latest = max((B.ts[-1] for (s, t), B in bot.store.items() if t == "1min" and len(B)), default=None)
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
    try:
        if a.poll:
            while True:
                ib.sleep(poll_iv)
                for s in syms:
                    pl = ib.reqHistoricalData(bot.contracts[s], endDateTime="", durationStr="1 D",
                                              barSizeSetting="1 min", whatToShow="TRADES",
                                              useRTH=True, formatDate=2, keepUpToDate=False)
                    bot.ingest(s, pl, report=True)
        else:
            ib.run()
    except KeyboardInterrupt:
        print("\n⛔ kill-switch — flattening…")
        bot.flatten("kill-switch")
        ib.sleep(2)
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
