"""
live_trader_tightflag.py — HIGH/LOW TIGHT FLAG order robot (IBKR paper)
=======================================================================
SEPARATE BOT for the tight-flag strategy. It shares NOTHING mutable with the
cup-and-handle robot (live_trader_ibkr.py): different clientId, its own ledger,
its own log, its own orderRef namespace — and, critically, it NEVER touches an
order or position it did not create (see COEXISTENCE below).

THE TRADE (frozen gate-(a) v3 rules, from pattern_detector_tightflag.CONFIG):
  bar1 = 09:30-09:35, bar2 = 09:35-09:40 (clock-aligned 5-min).
  side  = bar 1's colour: green -> LONG, red -> SHORT, doji -> no trade.
  gates = bar1 range >= 2 x bar2 range; overshoot cap 0.5 x bar2 range on the
          trade side; both setup bars must have traded all 5 minutes; a LONG
          also needs bar 1's high to have reclaimed YESTERDAY'S CLOSE.
  entry = MARKET at 09:40:01 (skipped if the print is already at/past the stop).
  stop  = bar 2's low (long) / high (short), placed as a resting STP order.
  R     = bar 2's range. NO take-profit — exit is the stop, or flat at 15:49.
  fly   = if favourable excursion reaches +1.75R during bar 3 or 4, the trailing
          stop arms; thereafter at the close of a qualifying bar (long: green +
          higher high; short: red + lower low) the STP is MODIFIED to the
          previous printed bar's low/high. Never retreats. If it never arms,
          the stop is never moved.

⚠️ DELAYED DATA CANNOT TRADE THIS STRATEGY.
  The entry is at 09:40:01 off the bar that closes at 09:40. On IBKR's free
  15-minute delayed feed that bar is not visible until ~09:55, so an "entry"
  would be a quarter of an hour late — a different trade entirely. --delayed is
  therefore PLUMBING-ONLY: it proves the pipeline (bars -> setup -> order ->
  ledger) end to end, and every signal is logged with its true lateness so the
  distortion is never hidden. Real trading needs the real-time bundle
  (IBKR US Securities Snapshot + Equity/Options add-on, ~$14.50/mo).

COEXISTENCE with live_trader_ibkr.py (READ THIS BEFORE RUNNING BOTH):
  Safe by construction here:
    * distinct clientId (default 18 vs the cup bot's 8) -> separate order
      sequences; each API client only sees the orders it placed.
    * this bot cancels ONLY its own order handles and closes ONLY the positions
      it opened. It never calls reqGlobalCancel() and never iterates
      ib.positions() to flatten the account.
    * its "is this symbol busy" check counts only ITS OWN positions/pendings.
    * own files: data/paper_fills_tightflag.csv, data/replay_trades_tightflag.csv,
      logs/tightflag_<date>.log. orderRef prefix "TF-".
  NOT safe yet — two things you must decide (see --help and the README notes):
    1. live_trader_ibkr.py's EOD flatten calls ib.reqGlobalCancel() and then
       market-closes EVERY open account position. Run both and the cup bot's
       15:49 flatten will cancel this bot's resting stop and close its position.
       Fixing that means editing the cup bot (scoping its flatten to its own
       orderRefs) — NOT done here, since that file is off-limits without your go.
    2. Symbol overlap: the cup strategy whitelists QQQ/SPY, which is exactly
       this strategy's universe. IBKR NETS positions per account, so if one bot
       is long QQQ and the other shorts it, the broker shows a smaller (or zero)
       position while both bots believe they hold a full one. Run them on
       DISJOINT symbols, or accept that overlap is unmanaged.
  Sizing: both bots size off the same account equity, so running both doubles
  intended account risk unless you halve --risk on each.

SAFETY: shadow by default (--arm places PAPER orders) · refuses live ports ·
  --arm requires the real-time feed unless you also pass --i-understand-delayed.

USAGE
  python3 live_trader_tightflag.py --replay                 # walk the last session, shadow
  python3 live_trader_tightflag.py QQQ --delayed            # live shadow on delayed data
  python3 live_trader_tightflag.py QQQ --arm                # PAPER orders (needs approval + real-time)
"""
from __future__ import annotations

import os, sys, argparse, csv
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from pattern_detector_tightflag import (CONFIG, cfg_width, detect, prev_close_gate, r_unit_for,
                                        entry_fill)
from live_trader_ibkr import read_watchlist   # shared TradingView-export discovery
                                              # (import only — cup files stay read-only)
from data_layer import Bars

ET = ZoneInfo("America/New_York")
LIVE_PORTS = {4001, 7496}
FILLS_CSV = "data/paper_fills_tightflag.csv"
# REAL-MARKET runs only (USER 2026-09-01: real and replayed trades must never share
# a file). shadow/armed sessions write here — what the bot actually saw and did in
# the market, one row per symbol-session, upserted. Replays and cache regressions
# NEVER write here: the official replayed record is data/replay_trades_tightflag.csv,
# owned by the record_tightflag.py evening pipeline. Broker fill confirmations (with
# slippage) are a third, separate file: data/paper_fills_tightflag.csv.
LEDGER_CSV = "data/live_trades_tightflag.csv"
REF_PREFIX = "TF"                      # every order this bot creates carries it
EOD = dtime(15, 49)
# The frozen labeler exits at the close of the LAST clock window that STARTS at or
# before 15:49 — which window that is depends on the configured bar width (15:45 on
# the old 5-min clock, 15:49 on 1-min bars), so it is DERIVED from CONFIG here.
# PIVOT BUG FIX 2026-09-04: this sat hardcoded at 15:45 after the 1-min switch, so
# live flattened four minutes before the backtest (SMMT 09-03: booked $17.17 off the
# 15:45 bar where the labeler books $17.21 off the 15:49 bar).
_EODM = 9 * 60 + 30 + ((EOD.hour * 60 + EOD.minute) - (9 * 60 + 30)) \
    // cfg_width(CONFIG) * cfg_width(CONFIG)
EOD_BAR_START = dtime(_EODM // 60, _EODM % 60)
ENTRY_T = dtime(9, 40)
DEFAULT_SYMS = ["QQQ"]                 # user's preferred test symbol (2026-07-27)


# ----------------------------------------------------------------------------
# clock-aligned 5-min aggregation (mirrors pattern_detector_tightflag.clock_5min)
# ----------------------------------------------------------------------------

class Clock5:
    """1-min bars in -> closed clock-aligned setup bars out (window width comes
    from CONFIG["timeframe"] — 1 minute since the 2026-09-02 decision; windows
    anchored to 09:30). Emits a bar only when the window is COMPLETE (a later
    window opened), so nothing downstream ever sees a partial bar — that is what
    keeps the live path faithful to the backtest."""
    WIDTH = None                                     # resolved from CONFIG at import (below)
    def __init__(self, sink):
        self.sink = sink
        self.k = None
        self.o = self.h = self.l = self.c = None
        self.v = 0.0
        self.n = 0                                   # 1-min bars folded in (coverage)
        self.ts = None

    def add(self, t, o, h, l, c, v):
        mod = t.hour * 60 + t.minute
        if mod < 9 * 60 + 30 or mod >= 16 * 60:
            return
        k = (mod - (9 * 60 + 30)) // self.WIDTH
        if self.k is None:
            self._start(k, t, o, h, l, c, v)
            return
        if k == self.k:
            self.h = max(self.h, h); self.l = min(self.l, l)
            self.c = c; self.v += v; self.n += 1
            return
        self._flush()
        self._start(k, t, o, h, l, c, v)

    def _start(self, k, t, o, h, l, c, v):
        start = 9 * 60 + 30 + self.WIDTH * k
        self.k, self.o, self.h, self.l, self.c, self.v, self.n = k, o, h, l, c, v, 1
        self.ts = t.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)

    def _flush(self):
        if self.k is not None:
            self.sink(self.k, self.ts, self.o, self.h, self.l, self.c, self.v, self.n)

    def close_day(self):
        self._flush()
        self.k = None


# ----------------------------------------------------------------------------
# the bot
# ----------------------------------------------------------------------------


Clock5.WIDTH = cfg_width(CONFIG)   # 1 since 2026-09-02 — single source of truth

class TightFlagTrader:
    RUN_MODE = "shadow"          # set per run: shadow | armed | replay | cache

    def __init__(self, ib, Order, MarketOrder, args):
        self.ib, self.Order, self.MarketOrder, self.a = ib, Order, MarketOrder, args
        self.cfg = dict(CONFIG)
        self.contracts: dict = {}
        self.prev_close: dict = {}                   # sym -> yesterday's close (long gate)
        self.bars: dict = {}                         # sym -> dict(k -> tuple) closed 5-min
        self.order5: dict = {}                       # sym -> [k, ...] in print order
        self.aggs: dict = {}
        self.state: dict = {}                        # sym -> live trade state
        self.done: dict = {}                         # sym -> True once the day is finished
        self.my_orders: dict = {}                    # ref -> {"stop": Trade, "entry": Trade}
        self.closed: list = []
        self.session_tally: list = []
        self._last_size_note = ""
        self.day = None
        self.log_path = self._log_path_for(datetime.now(ET).date())

    def _log_path_for(self, day) -> str:
        """Which log file a session narrates into.

        BUG FIX 2026-09-10: an offline --cache-day regression appended its narration
        to the CURRENT day's log — roll_day short-circuits when the preset day already
        matches, so the path chosen at construction (today) was never repointed, and a
        09-03 replay wrote 09-03 setups and fills into the 09-10 session record. Give
        cache regressions their own file instead: replayed output never shares a file
        with a real session's record (USER 2026-09-01), and pointing it at the
        REPLAYED day's log would be no better — that would rewrite history."""
        if self.RUN_MODE == "cache":
            return f"logs/cachereplay_{day:%Y-%m-%d}.log"
        return f"logs/tightflag_{day:%Y-%m-%d}.log"

    # ---- narration -------------------------------------------------------
    def say(self, line):
        print(line)
        try:
            os.makedirs("logs", exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(line.strip() + "\n")
        except OSError:
            pass

    def equity(self) -> float:
        nl = 0.0
        for v in self.ib.accountValues():
            if v.tag == "NetLiquidation" and v.currency == "USD":
                nl = float(v.value); break
        return self.a.base if self.a.base > 0 else nl

    def my_open_syms(self) -> set:
        """ONLY this bot's own live trades — deliberately NOT ib.positions(), which
        would also see the cup robot's positions and block us (or worse, invite us
        to act on them)."""
        return {s for s, st in self.state.items() if st.get("open")}

    def broker_qty(self, sym) -> float:
        """Signed size the BROKER shows for this symbol — the NET of every strategy
        trading it. Informational only: with both robots on the same symbol this is
        NOT our position, so it must never decide how much we close."""
        for p in self.ib.positions():
            if p.contract.symbol == sym:
                return float(p.position)
        return 0.0

    def check_external_close(self, sym, st, ts, last_px):
        """Did something outside this bot kill our trade?

        With both robots on ONE symbol the account position is a blend, so it cannot
        tell us anything about OUR position. The reliable strategy-local signal is our
        OWN stop order: we placed it, only we cancel it. If it turns up cancelled and
        we did not do it, an account-wide action (the cup robot's global cancel /
        kill-switch, or a manual flatten) has removed our protection — and most likely
        our position with it. Book the trade and stand down rather than manage a ghost."""
        if not self.a.arm or not st.get("open"):
            return False
        h = self.my_orders.get(self._ref(sym)) or {}
        tr = h.get("stop")
        if tr is None or st.get("closing"):
            return False
        status = getattr(getattr(tr, "orderStatus", None), "status", "")
        if status in ("Cancelled", "ApiCancelled", "Inactive"):
            # AUDIT FIX 2026-09-10: this used to book EXT at a BAR price on sight and
            # walk away. But an orders-only "cancel all" (or a rejected stop that goes
            # Inactive) kills the protection WITHOUT touching the shares — booking
            # there records a fiction and abandons a live, naked position, the exact
            # TER failure. Ask the broker first; only a flat account may be booked.
            net = self.broker_qty(sym)
            ours_long = st["side"] == "long"
            if (net > 0) if ours_long else (net < 0):
                self.say(f"  🚨 {sym} our STOP is {status} but the broker still shows "
                         f"{net:+.0f} — the position is NAKED. Closing at market now; "
                         f"the ledger will book that fill.")
                self.close_my_position(sym, st, f"stop {status}")
                return True
            self.say(f"  ⚠️ {sym} our STOP was cancelled by something outside this bot "
                     f"(global cancel / kill-switch / manual) and the broker shows "
                     f"{net:+.0f} — the position is already gone. Booking and standing down.")
            self._book(sym, st, last_px, "EXT", ts)
            return True
        return False

    # ---- sizing ----------------------------------------------------------
    def qty_for(self, entry: float, stop: float) -> int:
        risk_ps = abs(entry - stop)
        if risk_ps <= 0:
            return 0
        eq = self.equity()
        if eq <= 0:
            return 0
        q = int((eq * self.a.risk) / risk_ps)
        # BLOCKER FIX 2026-07-28: this strategy's stops are ~0.12% of price, so a naive
        # risk-% size demands a median 8.5x equity in notional (93% of trades exceed
        # Reg-T intraday). Cap notional ALWAYS, not only when asked.
        cap = self.a.max_notional if self.a.max_notional > 0 else 1.0
        qcap = int(eq * cap / max(entry, 0.01))
        if q > qcap:
            self._last_size_note = (f"size cut {q} -> {qcap} by the {cap:g}x notional cap "
                                    f"(risk-% alone wanted {q * entry / eq:.1f}x equity)")
            q = qcap
        else:
            self._last_size_note = ""
        return max(q, 0)

    # ---- orders (scoped: we only ever touch refs we created) -------------
    def _ref(self, sym) -> str:
        """Our internal book key for a symbol-day (NOT what goes on the order)."""
        return f"{REF_PREFIX}-{sym}-{self.day:%Y%m%d}"

    def _oref(self, sym, leg) -> str:
        """The orderRef stamped on a REAL order. AUDIT FIX 2026-09-10: both legs
        used to carry the identical ref, so a restart sweep could not tell an
        unfilled ENTRY landmine (cancel it) from the PROTECTIVE stop guarding live
        shares (never cancel it) — a short setup's entry is a SELL STP exactly like
        a long's protective stop. leg: E=entry, P=protective, C=close."""
        return f"{self._ref(sym)}-{leg}"

    def place_stop_entry(self, sym, side, level, stop):
        """Resting STOP-ENTRY at `level` (USER 2026-07-28). It is valid during bar 3
        only; on_5min cancels it at bar 3's close if it never triggered. The protective
        stop is attached the moment this fills (attach_protective_stop) — see the
        docstring there for why the two are not sent together."""
        c = self.contracts[sym]
        ref = self._ref(sym)
        act = "BUY" if side == "long" else "SELL"
        qty = self.qty_for(level, stop)
        if qty <= 0:
            self.say(f"  · {sym} not arming — size would be 0")
            return None
        o = self.Order(orderId=self.ib.client.getReqId(), action=act, orderType="STP",
                       totalQuantity=qty, auxPrice=round(level, 2), tif="DAY",
                       orderRef=self._oref(sym, "E"), transmit=True)
        tr = self.ib.placeOrder(c, o)
        tr.fillEvent += self._fill_logger("ENTRY", sym)
        tr.fillEvent += (lambda trade, fill: self._on_entry_fill(sym, trade, fill))
        self.my_orders.setdefault(ref, {})["entry"] = tr
        # enough context to manage the position even if the waiting state was
        # cleaned up before a late fill event arrived (fills are asynchronous)
        self.my_orders[ref]["ctx"] = dict(side=side, level=level, stop=stop)
        self.say(f"  📌 {sym} resting {act}-STOP {qty} @ ${level:.2f} placed")
        return tr

    def attach_protective_stop(self, sym, side, qty, stop):
        """Protective STP for a position that has just filled."""
        c = self.contracts[sym]
        ref = self._ref(sym)
        exit_act = "SELL" if side == "long" else "BUY"
        o = self.Order(orderId=self.ib.client.getReqId(), action=exit_act, orderType="STP",
                       totalQuantity=qty, auxPrice=round(stop, 2), tif="DAY",
                       orderRef=self._oref(sym, "P"), transmit=True)
        tr = self.ib.placeOrder(c, o)
        tr.fillEvent += self._fill_logger("STOP", sym)
        tr.fillEvent += (lambda trade, fill: self._on_exit_fill(sym, trade, fill, "STOP"))
        self.my_orders.setdefault(ref, {})["stop"] = tr
        return tr

    def order_health(self, sym) -> str:
        """BLOCKER FIX 2026-07-28: nothing used to read orderStatus, so a REJECTED
        entry (margin/halt) or a REJECTED protective stop was completely silent.
        Returns '' when healthy, else a human reason."""
        h = self.my_orders.get(self._ref(sym)) or {}
        for leg in ("entry", "stop"):
            tr = h.get(leg)
            if tr is None:
                continue
            stt = getattr(getattr(tr, "orderStatus", None), "status", "")
            if stt in ("Inactive", "ApiCancelled", "Cancelled"):
                return f"{leg} order is {stt}"
        return ""

    def assert_protected(self, sym, st, ts):
        """An open position MUST have a live protective stop. If it does not, close
        the position at market rather than book a fiction and walk away."""
        if not self.a.arm or not st.get("open"):
            return True
        if st.get("closing"):
            # AUDIT FIX 2026-09-10: close_my_position cancels our protective stop
            # BEFORE sending its market close, so from here the stop looks "missing"
            # on every later bar until the close fills. Without this guard a queued
            # close (LULD halt, thin book) got another FULL-SIZE market order every
            # bar — three bars of a halt, three closes, and the account ends up short
            # the position it was flattening. A close is in flight: leave it alone.
            return True
        h = self.my_orders.get(self._ref(sym)) or {}
        tr = h.get("stop")
        stt = getattr(getattr(tr, "orderStatus", None), "status", "") if tr else "missing"
        if tr is None or stt in ("Inactive", "ApiCancelled", "Cancelled"):
            self.say(f"  🚨 {sym} protective stop is {stt} while the position is OPEN — "
                     f"closing at market now (never leave it naked)")
            self.close_my_position(sym, st, f"stop {stt}")
            return False
        return True

    @staticmethod
    def _is_done(tr) -> bool:
        """Trade in one of ib_async's DoneStates — modifying or cancelling it raises."""
        return getattr(getattr(tr, "orderStatus", None), "status", "") in (
            "Filled", "Cancelled", "ApiCancelled", "Inactive")

    def move_stop(self, sym, new_stop):
        ref = self._ref(sym)
        h = self.my_orders.get(ref)
        if not h:
            return
        tr = h.get("stop")
        if tr is None or self._is_done(tr):
            return                                     # exited/dead — nothing to trail
        o = tr.order
        o.auxPrice = round(new_stop, 2)
        self.ib.placeOrder(self.contracts[sym], o)     # same orderId = modify

    def cancel_my_stop(self, sym):
        h = self.my_orders.get(self._ref(sym))
        tr = h.get("stop") if h else None
        if tr is not None and not self._is_done(tr):
            try:
                self.ib.cancelOrder(tr.order)
            except Exception:
                pass

    def cancel_my_entry(self, sym):
        """BUG FIX 2026-09-09: the no_trigger cleanup used to call cancel_my_stop —
        the PROTECTIVE leg, which does not even exist before a fill — so the unfilled
        resting ENTRY stayed working until the DAY expiry (KLAC 09-09: a live
        228-share buy-stop nobody was watching). Cancel the leg that was meant."""
        h = self.my_orders.get(self._ref(sym))
        tr = h.get("entry") if h else None
        if tr is not None and not self._is_done(tr):
            try:
                self.ib.cancelOrder(tr.order)
            except Exception:
                pass

    def _resize_stop(self, sym, qty):
        """A partial entry topped up — the protective stop must cover what we hold."""
        h = self.my_orders.get(self._ref(sym)) or {}
        tr = h.get("stop")
        if tr is not None and not self._is_done(tr):
            tr.order.totalQuantity = qty
            self.ib.placeOrder(self.contracts[sym], tr.order)

    def _now(self):
        return datetime.now(ET).replace(tzinfo=None)

    @staticmethod
    def _fill_ts(fill):
        t = getattr(fill, "time", None)
        if t is None:
            return None
        return t.astimezone(ET).replace(tzinfo=None) if getattr(t, "tzinfo", None) else t

    @staticmethod
    def _exec_truth(trade, fill):
        """(cumulative qty, average price) for THIS order, read from the EXECUTION.

        AUDIT FIX 2026-09-10 — the single most dangerous bug in the armed redesign.
        ib_async emits fillEvent from execDetails and does NOT update
        trade.orderStatus there: filled / remaining / avgFillPrice are written by a
        SEPARATE wire message whose ordering TWS does not guarantee. Reading them in
        a fill handler is a coin flip — they can still hold the previous tranche, or
        the 0/0 defaults. execution.cumQty and execution.avgPrice are cumulative for
        the order and correct at emit time; trade.fills is appended before the emit,
        so summing it is the belt-and-braces fallback (it is how ib_async's own
        Trade.filled() is defined)."""
        ex = fill.execution
        qty = int(getattr(ex, "cumQty", 0) or 0)
        if qty <= 0:
            fills = list(getattr(trade, "fills", None) or [fill])
            qty = int(sum(float(f.execution.shares) for f in fills))
        px = float(getattr(ex, "avgPrice", 0) or 0) or float(ex.price)
        return qty, px

    @classmethod
    def _exec_complete(cls, trade, fill):
        """Is the ORDER now fully filled? Same reasoning as _exec_truth: compare the
        execution's cumulative quantity against the order's own size, never
        orderStatus.remaining (which reads 0 both before the first status message
        AND after completion — indistinguishable, and it made every armed exit
        wedge open for the rest of the day)."""
        want = float(getattr(trade.order, "totalQuantity", 0) or 0)
        got, _ = cls._exec_truth(trade, fill)
        return want <= 0 or got + 1e-9 >= want

    def _on_entry_fill(self, sym, trade, fill):
        """ARMED TRUTH (2026-09-09): a position EXISTS when IBKR fills it, at IBKR's
        price — never when a bar touches a level. TER 09-09: the bars booked a
        fictional 378.00 entry and 375.01 exit while the account actually bought
        81 @ 380.04 (+204c slip) and kept them, unprotected."""
        if not self.a.arm:
            return
        ref = self._ref(sym)
        ctx = (self.my_orders.get(ref) or {}).get("ctx") or {}
        qty, px = self._exec_truth(trade, fill)
        ts = self._fill_ts(fill) or self._now()
        st = self.state.get(sym)
        if st is None:
            # the window-end cleanup (or a completed trade) raced this fill. AUDIT FIX
            # 2026-09-10: cumQty counts EVERY share this order ever filled, including
            # any we have already exited, so size the adoption from what the broker
            # actually shows — and if it shows nothing, there is no position to adopt.
            net = self.broker_qty(sym)
            side = ctx.get("side", "long")
            live = int(abs(net)) if ((net > 0) if side == "long" else (net < 0)) else 0
            if live <= 0:
                self.say(f"  ℹ️ {sym} a late entry fill arrived but the broker shows no "
                         f"{side} position ({net:+.0f}) — nothing to adopt, standing down")
                return
            self.say(f"  🚨 {sym} broker filled AFTER the window cleanup — adopting the "
                     f"{live} share(s) it actually shows and managing them "
                     f"(real money beats tidy state)")
            qty = min(qty, live)
            st = self.state[sym] = dict(side=side, setup=None,
                                        entry_level=ctx.get("level", 0.0),
                                        fly=False, trail=0, mfe=0.0)
            self.done.pop(sym, None)
        first = not st.get("open")
        stop = st.get("stop") if not first else ctx.get("stop")
        if stop is None:
            setup = st.get("setup") or {}
            stop = setup.get("l2") if st["side"] == "long" else setup.get("h2")
        st.update(open=True, entry=px, stop=stop, qty=qty, entry_ts=ts, fill_k=None,
                  R=max(abs(px - stop), 1e-9), late=0)
        st.setdefault("mfe", 0.0); st.setdefault("fly", False); st.setdefault("trail", 0)
        if first:
            self.say(f"  ▶ {sym} BROKER FILLED {st['side'].upper()} {qty} @ ${px:.2f} "
                     f"(level ${st.get('entry_level', 0.0):.2f})  stop ${stop:.2f}  "
                     f"R ${st['R']:.2f}  [🔴 ARMED]")
            self.attach_protective_stop(sym, st["side"], qty, stop)
        else:
            self._resize_stop(sym, qty)

    def _on_exit_fill(self, sym, trade, fill, kind):
        """ARMED TRUTH: the exit is booked from the broker's fill — its price, its
        time — never from a bar touching the stop level."""
        st = self.state.get(sym)
        if st is None or not st.get("open"):
            return
        if not self._exec_complete(trade, fill):
            _q, _p = self._exec_truth(trade, fill)
            self.say(f"  … {sym} {kind} partial: {_q:.0f} of "
                     f"{float(trade.order.totalQuantity):.0f} filled — still exiting")
            return                                     # partial — wait for the rest
        _q, px = self._exec_truth(trade, fill)
        self._book(sym, st, px, kind, self._fill_ts(fill) or self._now())

    def shutdown_report(self):
        """ARMED shutdown: cancel unfilled entry landmines, KEEP protective stops,
        and shout about any real position left behind."""
        for ref, h in self.my_orders.items():
            tr = h.get("entry")
            if tr is not None and getattr(getattr(tr, "orderStatus", None), "status", "") \
                    not in ("Filled", "Cancelled", "ApiCancelled"):
                try:
                    self.ib.cancelOrder(tr.order)
                    self.say(f"  🧹 cancelled the unfilled resting entry {ref}")
                except Exception:
                    pass
        for sym, st in list(self.state.items()):
            if st.get("open"):
                self.say(f"  🚨 SHUTDOWN WITH AN OPEN POSITION: {sym} {st['side']} "
                         f"{st.get('qty', '?')} @ ${st.get('entry', 0.0):.2f} — the "
                         f"protective stop is left WORKING but it is a DAY order (dies "
                         f"at the close). FLATTEN MANUALLY AT IBKR.")

    def close_my_position(self, sym, st, why):
        """Market-close THIS bot's own position.

        BLOCKER FIX 2026-07-28: the size is clamped to what the broker actually shows
        when that is SAFE to do — i.e. when the account position is on our side and no
        larger than our own book. Previously the docstring promised a clamp the code
        did not implement, so a 15:49 race with the cup robot (which flattens the whole
        account) could fire our SELL into a flat account and open an unmonitored,
        unprotected REVERSE position overnight.

        On a SHARED symbol the account net is a blend, so it cannot size us. The rule:
          * net is flat, or on the opposite side  -> send NOTHING (we are already out,
            or what remains belongs to the other strategy)
          * net on our side  -> close min(our size, |net|)
        """
        st["closing"] = True
        self.cancel_my_stop(sym)
        self.cancel_my_entry(sym)
        want = int(st.get("qty") or 0)
        if not (self.a.arm and want > 0):
            self.say(f"  ⛔ {sym} closing own position ({why})")
            return False
        net = self.broker_qty(sym)
        ours_long = st["side"] == "long"
        same_side = (net > 0) if ours_long else (net < 0)
        if not same_side:
            self.say(f"  ⚠️ {sym} NOT sending a close ({why}) — broker shows {net:+.0f}, "
                     f"which is flat or opposite to our {'long' if ours_long else 'short'}. "
                     f"Something already closed us; sending an order would OPEN a reverse position.")
            if st.get("open"):
                # the position is gone at the broker — record reality, never a ghost
                B = self.bars.get(sym, {})
                px = B[self.order5[sym][-1]]["c"] if self.order5.get(sym) else st.get("entry", 0.0)
                self._book(sym, st, px, "EXT", self._now())
            return False
        qty = int(min(want, abs(net)))
        act = "SELL" if ours_long else "BUY"
        o = self.MarketOrder(act, qty)
        o.orderRef = self._oref(sym, "C")
        kind = "EOD" if why.startswith("EOD") else "EXT"
        tr = self.ib.placeOrder(self.contracts[sym], o)
        try:
            tr.fillEvent += self._fill_logger("EXIT", sym)
            tr.fillEvent += (lambda trade, fill, k=kind: self._on_exit_fill(sym, trade, fill, k))
        except Exception:
            pass
        extra = "" if qty == want else f"  (clamped from {want}; broker net {net:+.0f})"
        self.say(f"  ⛔ {sym} closing OUR {qty} ({why}){extra}")
        return True

    def _fill_logger(self, kind, sym):
        def on_fill(trade, fill):
            o = trade.order
            level = o.auxPrice if o.orderType == "STP" else fill.execution.price
            px = fill.execution.price
            slip = (px - level) if o.action == "BUY" else (level - px)
            self.say(f"  💰 FILL {kind:5} {sym} {fill.execution.shares:.0f}@${px:.2f} "
                     f"(level ${level:.2f}, slip {slip*100:+.1f}¢)")
            os.makedirs("data", exist_ok=True)
            new = not os.path.exists(FILLS_CSV)
            with open(FILLS_CSV, "a") as f:
                if new:
                    f.write("time,symbol,ref,leg,action,shares,level,fill,slip_cents\n")
                f.write(f"{fill.time},{sym},{o.orderRef},{kind},{o.action},"
                        f"{fill.execution.shares:.0f},{level:.2f},{px:.2f},{slip*100:.1f}\n")
        return on_fill

    # ---- the strategy ----------------------------------------------------
    def roll_day(self, day):
        """BLOCKER FIX 2026-07-28: a new session wipes ALL per-day state.
        Without this, a bot started before 09:30 seeds YESTERDAY as 'today', keeps
        yesterday's setup in self.state, and the k==1 guard then skips today's
        evaluation entirely — entering today's session on yesterday's levels."""
        if self.day == day:
            return
        if self.day is not None:
            stale = [s for s, st in self.state.items() if st.get("open")]
            if stale:
                self.say(f"  ⚠️ session rollover {self.day} -> {day} with {len(stale)} "
                         f"position(s) still open: {', '.join(stale)} — booking them now")
                self.eod(datetime.combine(self.day, EOD_BAR_START))
            self.say(f"  🔄 new session {day} — clearing per-day state")
        self.day = day
        self.bars.clear(); self.order5.clear(); self.state.clear(); self.done.clear()
        self.my_orders.clear()
        for agg in self.aggs.values():
            agg.k = None
        self.log_path = self._log_path_for(day)

    def on_5min(self, sym, k, ts, o, h, l, c, v, cov):
        """One CLOSED clock-aligned 5-min bar. k = window index (0 = 09:30)."""
        self.roll_day(ts.date())
        B = self.bars.setdefault(sym, {})
        B[k] = dict(k=k, ts=ts, o=o, h=h, l=l, c=c, v=v, cov=cov)
        self.order5.setdefault(sym, []).append(k)
        st = self.state.get(sym)

        # ---- decision point: bar 2 (09:35-09:40) has just closed ----
        if k == 1 and sym not in self.state and not self.done.get(sym):
            self._evaluate(sym)
            return

        if not st:
            return

        # ---- the entry window is over: retire an unfilled setup --------------
        # AUDIT FIX 2026-09-10: the cleanup below only fires on the single bucket
        # that STRADDLES the deadline (09:44). IBKR omits a 1-min bar whenever the
        # symbol does not trade during it — routine on thin gappers — and when the
        # missing one is 09:44 the cleanup never runs: the resting DAY order stays
        # live and unattended for the rest of the session, free to fill hours late.
        # That is the 09-09 landmine. Any bar at/after the deadline retires it.
        _w = Clock5.WIDTH
        _dead = self.cfg.get("entry_deadline_min", 15)
        if not st.get("open") and k * _w >= _dead:
            self.done[sym] = True
            self.state.pop(sym, None)
            self.cancel_my_entry(sym)
            self.say(f"  · {sym} no trade — entry window closed before a fill "
                     f"(no_trigger; swept at {ts:%H:%M})")
            return

        # ---- resolve the resting STOP-ENTRY (bar 3 .. 09:45, USER 2026-09-02) ----
        if (not st.get("open") and k >= self.cfg.get("trigger_bar", 2)
                and k * _w < _dead):
            lvl, lng = st["entry_level"], st["side"] == "long"
            if lng:
                px = o if o >= lvl else (lvl if h >= lvl else None)
            else:
                px = o if o <= lvl else (lvl if l <= lvl else None)
            if self.a.arm:
                # ARMED TRUTH (2026-09-09): a bar touching the level is NOT an entry —
                # only the broker's fill is (_on_entry_fill). Bars only decide when the
                # unfilled resting order dies.
                if not st.get("open"):
                    if (k + 1) * _w >= _dead:          # that was the last eligible bucket
                        self.done[sym] = True
                        self.state.pop(sym, None)
                        self.cancel_my_entry(sym)      # pull the unfilled entry order
                        why2 = ("bars touched the level but the order never filled"
                                if px is not None else f"never reached ${lvl:.2f}")
                        self.say(f"  · {sym} no trade — {why2} by 09:45 (no_trigger)")
                    return
                # broker filled -> fall through and manage the REAL position
            else:
                if px is None:
                    if (k + 1) * _w >= _dead:          # that was the last eligible bucket
                        self.done[sym] = True
                        self.state.pop(sym, None)
                        self.say(f"  · {sym} no trade — never reached ${lvl:.2f} by 09:45 (no_trigger)")
                    return                             # else: keep resting into the next bar
                self._open_trade(sym, st, px, ts)
                if st.get("open"):
                    st["fill_k"] = k   # same-bar stop-outs price at the stop level
            # fall through: this same bar 3 is also managed (stop-first convention)

        if not st.get("open"):
            return

        # ---- manage an open trade ----
        if self.check_external_close(sym, st, ts, c):
            return
        if not self.assert_protected(sym, st, ts):
            if not self.a.arm:
                self._book(sym, st, c, "EXT", ts)
            return          # armed: the market close it sent (or its fallback) books
        lng = st["side"] == "long"
        R = st["R"]
        fav = ((h - st["entry"]) if lng else (st["entry"] - l)) / R
        st["mfe"] = max(st.get("mfe", 0.0), fav)
        hit = (l <= st["stop"]) if lng else (h >= st["stop"])
        if hit:
            if self.a.arm:
                # ARMED TRUTH: a bar touching the stop is not an exit — the broker's
                # stop fill books it (_on_exit_fill), at the real price and time.
                pass
            else:
                if st.get("fill_k") == k:
                    px = st["stop"]    # same-bar stop-out: the open predates our fill
                else:
                    px = min(st["stop"], o) if lng else max(st["stop"], o)
                self._book(sym, st, px, "STOP", ts)
                return
        barnum = k + 1
        if not st["fly"] and barnum <= self.cfg["fly_by_bar"] and fav >= self.cfg["fly_trigger_R"]:
            st["fly"] = True
            self.say(f"  🚀 {sym} FLY ARMED at bar {barnum} (+{fav:.2f}R) — trailing stop is live")
        lag = self.cfg["trail_lag_bars"] - 1
        seq = self.order5[sym]
        pos = seq.index(k)
        if st["fly"] and pos >= lag and barnum >= self.cfg["trail_from_bar"]:
            prev = B[seq[pos - lag]]
            if lng:
                ok = c > o and h > prev["h"]
                new = prev["l"]
                better = new > st["stop"]
            else:
                ok = c < o and l < prev["l"]
                new = prev["h"]
                better = new < st["stop"]
            if ok and better:
                st["stop"] = new
                st["trail"] += 1
                self.say(f"  🪜 {sym} trail #{st['trail']} -> stop ${new:.2f} "
                         f"(low/high of the {prev['ts']:%H:%M} bar)")
                if self.a.arm:
                    self.move_stop(sym, new)
        if ts.time() >= EOD_BAR_START:                 # the EOD exit bar just closed -> flat
            if self.a.arm:
                if not st.get("closing"):
                    self.close_my_position(sym, st, "EOD 15:49")   # its FILL books the exit
            else:
                self._book(sym, st, c, "EOD", ts)

    def _evaluate(self, sym):
        B = self.bars.get(sym, {})
        if 0 not in B or 1 not in B:
            self.done[sym] = True
            self.say(f"  · {sym} no trade — missing an opening bar")
            return
        b0, b1 = B[0], B[1]
        five = Bars(sym, self.day, CONFIG["timeframe"], [b0["ts"], b1["ts"]],
                    [b0["o"], b1["o"]], [b0["h"], b1["h"]], [b0["l"], b1["l"]],
                    [b0["c"], b1["c"]], [b0["v"], b1["v"]], True)
        setup, why = detect(five, [0, 1], [b0["cov"], b1["cov"]], self.cfg)
        if setup is None:
            self.done[sym] = True
            self.say(f"  · {sym} no trade — {why}")
            return
        pc = self.prev_close.get(sym)
        if prev_close_gate(setup, pc, self.cfg):
            self.done[sym] = True
            self.say(f"  · {sym} no trade — long_below_prev_close "
                     f"(bar1 high ${setup['h1']:.2f} < prev close ${pc:.2f})")
            return
        if self.a.arm:
            # LATE-ARM GUARD (2026-09-09): placing a stop-entry for a window that is
            # already over is a DIFFERENT trade — at 12:07 a morning buy-stop is just
            # a marketable order (TER: instant fill @ 380.04, +204c of slip, 68% of R
            # gone before the trade began). A late start forfeits the day's entry.
            dead = (datetime.combine(self.day, dtime(9, 30))
                    + timedelta(minutes=self.cfg.get("entry_deadline_min", 15)))
            now = self._now()
            if now >= dead:
                self.done[sym] = True
                self.say(f"  ⏰ {sym} setup found, but its entry window ended {dead:%H:%M} "
                         f"and it is now {now:%H:%M} — LATE START: no order placed, no row")
                return
            if len(self.state) >= self.a.max_positions:
                self.done[sym] = True
                self.say(f"  · {sym} skipped — {self.a.max_positions} setup(s) already "
                         f"working (armed orders are real the moment they rest)")
                return
        lvl = setup["entry_level"]
        stop0 = setup["l2"] if setup["side"] == "long" else setup["h2"]
        self.state[sym] = dict(side=setup["side"], setup=setup, armed_at=b1["ts"], open=False,
                               fly=False, trail=0, mfe=0.0, entry_level=lvl)
        self.say(f"  🎯 {sym} SETUP {setup['side'].upper()}  ratio {setup['ratio']:.2f}:1  "
                 f"bar2 range ${setup['range2']:.2f}\n"
                 f"      resting {'BUY' if setup['side']=='long' else 'SELL'}-STOP @ ${lvl:.2f}"
                 f"   stop ${stop0:.2f}   R ${abs(lvl-stop0):.2f}"
                 f"   — valid from bar 3 until 09:45")
        if self.a.arm:
            self.place_stop_entry(sym, setup["side"], lvl, stop0)

    def _open_trade(self, sym, st, price, ts):
        """MODELED fill (shadow/replay/cache): the resting stop-entry filled at `price`
        (the level, or the bucket open if the market gapped through the order).
        ARMED runs never come here — the broker's fill opens the trade (_on_entry_fill)."""
        if self.a.arm:
            self.say(f"  🐞 {sym} _open_trade called while ARMED — refusing the modeled "
                     f"fill (broker fills are the only armed truth)")
            return
        setup = st["setup"]
        lng = st["side"] == "long"
        stop = setup["l2"] if lng else setup["h2"]
        if len(self.my_open_syms()) >= self.a.max_positions:
            self.done[sym] = True
            self.state.pop(sym, None)
            self.cancel_my_stop(sym)
            self.say(f"  · {sym} skipped — own position cap ({self.a.max_positions}) reached")
            return
        R = r_unit_for(price, stop, setup["range2"], self.cfg)
        if R <= 0:
            self.done[sym] = True; self.state.pop(sym, None)
            self.say(f"  · {sym} no trade — zero risk distance"); return
        gapped = (price > st["entry_level"] + 1e-9) if lng else (price < st["entry_level"] - 1e-9)
        qty = self.qty_for(price, stop)
        st.update(open=True, entry=price, stop=stop, R=R, qty=qty, entry_ts=ts,
                  late=0, fill_k=None)
        tag = "🔴 ARMED" if self.a.arm else "🟢 shadow"
        note = f"  ⚠️ GAPPED through the order (level ${st['entry_level']:.2f})" if gapped else ""
        self.say(f"  ▶ {sym} FILLED {st['side'].upper()} {qty} @ ${price:.2f}  "
                 f"stop ${stop:.2f}  R ${R:.2f}  [{tag}]{note}")
        if self._last_size_note:
            self.say(f"      ✂️ {sym} {self._last_size_note}")
        if qty <= 0:
            self.done[sym] = True
            st["open"] = False
            self.state.pop(sym, None)         # dead for the day — never re-resolve entry
            self.say(f"  · {sym} STOOD DOWN — size resolved to 0 shares (equity/cap); no trade")
            return
        if self.a.arm and qty > 0:
            self.attach_protective_stop(sym, st["side"], qty, stop)

    def _book(self, sym, st, px, kind, ts):
        lng = st["side"] == "long"
        r = ((px - st["entry"]) if lng else (st["entry"] - px)) / st["R"]
        self.say(f"  {'🩸' if r < 0 else '🎉'} {sym} EXIT {kind} @ ${px:.2f}  {r:+.2f}R  "
                 f"(peak +{st.get('mfe',0):.2f}R, trail x{st['trail']}, "
                 f"{'FLY' if st['fly'] else 'no fly'})")
        self.closed.append(dict(ts=ts, sym=sym, side=st["side"], entry=st["entry"],
                                stop_final=st["stop"], exit=px, kind=kind, r=r,
                                R=st["R"], qty=st.get("qty", 0), fly=st["fly"],
                                trail=st["trail"], mfe=st.get("mfe", 0.0),
                                late=st.get("late", 0)))
        if self.a.arm:
            # ARMED TRUTH (2026-09-09): armed bookings are now driven BY broker fills
            # (_on_exit_fill / close_my_position), so there is nothing to verify here —
            # just guarantee a booked trade leaves NOTHING working at the broker.
            self.cancel_my_stop(sym)
            self.cancel_my_entry(sym)
        st["open"] = False
        # BUG FIX 2026-09-04: one setup, one trade. Leaving the symbol in self.state
        # after booking kept the entry-resolution branch armed — a re-touch of the
        # level before 09:45 after a same-window stop-out would have opened a SECOND
        # trade, and the window's end printed a bogus "no_trigger" for a symbol that
        # DID trade (TGTX 09-03).
        self.done[sym] = True
        self.state.pop(sym, None)
        if self.a.arm:
            # AUDIT FIX 2026-09-10: bookings used to sit in memory until eod()/Ctrl-C,
            # so a crash (or a Gateway drop) between a 10:02 stop-out and 15:49 lost
            # the day's real trades entirely. write_ledger upserts per symbol-session,
            # so calling it per booking is idempotent — just durable.
            self.write_ledger()

    # ---- EOD -------------------------------------------------------------
    def eod(self, ts=None):
        ts = ts or datetime.now(ET).replace(tzinfo=None)
        if self.a.arm:
            open_syms = [s2 for s2, st2 in list(self.state.items()) if st2.get("open")]
            for s2 in open_syms:
                if not self.state[s2].get("closing"):
                    self.close_my_position(s2, self.state[s2], "EOD flatten")
            if open_syms:
                try:
                    self.ib.sleep(5)               # let the close fills arrive and book
                except Exception:
                    pass
            for s2 in open_syms:                   # never leave the ledger silent
                st2 = self.state.get(s2)
                if not (st2 and st2.get("open")):
                    continue
                # AUDIT FIX 2026-09-10: only a broker that shows us FLAT may be booked.
                # ib.sleep() above is a no-op when eod() is reached from inside an
                # event callback, so "the fill has not arrived" is often just "we did
                # not actually wait" — booking there would re-invent the very fiction
                # this redesign removes. If the shares are still there, say so and
                # leave the trade open; the main loop's 15:52 check calls eod() again.
                net2 = self.broker_qty(s2)
                if (net2 > 0) if st2["side"] == "long" else (net2 < 0):
                    if not st2.get("eod_shouted"):
                        st2["eod_shouted"] = True
                        self.say(f"  🚨 {s2} STILL OPEN at the broker after the EOD close "
                                 f"({net2:+.0f}) — NOT booking a guess. Leaving the trade "
                                 f"open for the next flatten attempt; if it survives the "
                                 f"session, FLATTEN IT MANUALLY AT IBKR.")
                    continue
                B2 = self.bars.get(s2, {})
                px2 = (B2[self.order5[s2][-1]]["c"] if self.order5.get(s2)
                       else st2.get("entry", 0.0))
                self.say(f"  🚨 {s2} broker shows flat but the close fill never reached "
                         f"us — booking EXT at last ${px2:.2f}; CHECK IBKR for the real fill")
                self._book(s2, st2, px2, "EXT", ts)
            self.write_ledger()
            return
        for sym, st in list(self.state.items()):
            if st.get("open"):
                # fallback path — reached when no 15:45 bar exists (half-day sessions
                # close at 13:00). Book at the LAST bar that actually printed, and stamp
                # its real time rather than a notional 15:49.
                B = self.bars.get(sym, {})
                if self.order5.get(sym):
                    lastbar = B[self.order5[sym][-1]]
                    self._book(sym, st, lastbar["c"], "EOD", lastbar["ts"])
                else:
                    self._book(sym, st, st["entry"], "EOD", ts)
        self.write_ledger()

    def write_ledger(self):
        """UPSERT one row per (symbol, session-date).

        The ledger used to be a blind append log, so replaying the same day — or
        re-running a regression sweep — silently stacked duplicate rows for the same
        trade. One trade, one row: a re-run REPLACES its own row rather than adding
        another, so live and replay of the same session can never double-count."""
        if not self.closed:
            return
        if self.RUN_MODE not in ("shadow", "armed"):
            # replay/cache results are NOT market activity — they belong to the
            # record pipeline (data/replay_trades_tightflag.csv) or to nothing at all.
            tot = sum(c["r"] for c in self.closed)
            self.session_tally.extend(self.closed)
            self.say(f"\n  📒 {len(self.closed)} {self.RUN_MODE} trade(s), {tot:+.2f}R — "
                     f"NOT written to the live ledger (replayed data records via "
                     f"record_tightflag.py)")
            self.closed = []
            return
        os.makedirs("data", exist_ok=True)
        cols = ["time","symbol","side","entry","exit","exit_kind","R_unit","pnl_R","qty",
                "fly","trail_moves","mfe_R","entry_late_min","mode"]
        rows: dict[tuple, dict] = {}
        if os.path.exists(LEDGER_CSV):
            with open(LEDGER_CSV, newline="") as f:
                for r in csv.DictReader(f):
                    rows[(r.get("symbol",""), str(r.get("time",""))[:10])] = r
        added = replaced = 0
        for c in self.closed:
            key = (c["sym"], str(c["ts"])[:10])
            if key in rows:
                replaced += 1
            else:
                added += 1
            rows[key] = dict(zip(cols, [
                c["ts"], c["sym"], c["side"], f"{c['entry']:.2f}", f"{c['exit']:.2f}",
                c["kind"], f"{c['R']:.4f}", f"{c['r']:.3f}", c["qty"], int(c["fly"]),
                c["trail"], f"{c['mfe']:.2f}", c["late"], self.RUN_MODE]))
        with open(LEDGER_CSV, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for k in sorted(rows, key=lambda k: (k[1], k[0])):
                w.writerow(rows[k])
        tot = sum(c["r"] for c in self.closed)
        note = f"{added} new" + (f", {replaced} updated" if replaced else "")
        self.session_tally.extend(self.closed)
        self.say(f"\n  📒 {len(self.closed)} trade(s) [{note}] appended to "
                 f"{os.path.abspath(LEDGER_CSV)}   {tot:+.2f}R "
                 f"(file now holds {len(rows)} trade(s))")
        self.closed = []

    def run_summary(self):
        """One line per trade + the day's total across EVERY symbol scanned — otherwise a
        10-ticker replay prints a separate 'total' per symbol and none of them is the day."""
        t = self.session_tally
        print(f"\n{'='*64}\n  RUN SUMMARY — {len(t)} trade(s) across all symbols scanned")
        if not t:
            print("  (no setups qualified)\n" + "="*64)
            return
        for c in sorted(t, key=lambda x: str(x["ts"])):
            print(f"   {str(c['ts'])[:16]}  {c['sym']:<5} {c['side']:<5} "
                  f"{c['kind']:<4} {c['r']:+6.2f}R   peak +{c['mfe']:.2f}R"
                  f"{'  FLY x%d' % c['trail'] if c['fly'] else ''}")
        tot = sum(c["r"] for c in t)
        wins = sum(1 for c in t if c["r"] > 0)
        print(f"   {'-'*58}\n   TOTAL {tot:+.2f}R over {len(t)} trade(s), {wins} winner(s)\n{'='*64}")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def show_trades(a):
    """Print the trade ledger, ONE row per (symbol, session) however many times that
    session was run. Live and replay of the same day are the same trade, so they
    collapse to a single line; the mode column shows which run last wrote it."""
    if not os.path.exists(LEDGER_CSV):
        print(f"no ledger yet at {LEDGER_CSV}")
        return
    with open(LEDGER_CSV, newline="") as f:
        rows = list(csv.DictReader(f))
    keep = {}
    for r in rows:                                     # de-dup defensively
        keep[(r["symbol"], str(r["time"])[:10])] = r
    rows = sorted(keep.values(), key=lambda r: (str(r["time"]), r["symbol"]))
    if a.mode_filter:
        want = {m.strip() for m in a.mode_filter.split(",")}
        rows = [r for r in rows if r.get("mode") in want]
    if a.since:
        rows = [r for r in rows if str(r["time"])[:10] >= a.since]
    if not rows:
        print("no trades match the filter"); return
    print(f"\n  {os.path.abspath(LEDGER_CSV)}")
    print(f"  TIGHT-FLAG TRADES — {len(rows)} (one row per symbol-session)")
    print(f"  {'date':<11}{'time':<7}{'sym':<6}{'side':<6}{'entry':>9}{'exit':>9}"
          f"{'kind':>6}{'R$':>7}{'pnl_R':>8}{'peak':>7}{'trail':>6}  mode")
    print("  " + "-" * 92)
    tot = 0.0
    for r in rows:
        pnl = float(r["pnl_R"]); tot += pnl
        fly = "FLY" if r["fly"] in ("1", 1, True, "True") else ""
        print(f"  {str(r['time'])[:10]:<11}{str(r['time'])[11:16]:<7}{r['symbol']:<6}"
              f"{r['side']:<6}{float(r['entry']):>9.2f}{float(r['exit']):>9.2f}"
              f"{r['exit_kind']:>6}{float(r['R_unit']):>7.2f}{pnl:>+8.2f}"
              f"{float(r['mfe_R']):>+7.2f}{(fly + ' x' + str(r['trail_moves'])) if fly else '':>6}"
              f"  {r.get('mode','')}")
    wins = sum(1 for r in rows if float(r["pnl_R"]) > 0)
    print("  " + "-" * 92)
    print(f"  TOTAL {tot:+.2f}R over {len(rows)} trade(s) · {wins} winner(s) "
          f"({100*wins/len(rows):.0f}%) · avg {tot/len(rows):+.3f}R")
    by = {}
    for r in rows:
        by.setdefault(r.get("mode",""), []).append(float(r["pnl_R"]))
    print("  by run type: " + " · ".join(f"{k or '?'} {len(v)} trades {sum(v):+.1f}R"
                                         for k, v in sorted(by.items())))


def cache_replay(a, syms):
    """Drive the LIVE decision path with the exact 5-min bars the backtest scored
    (cache/ibkr5), so the two can be diffed bar for bar. No broker connection."""
    import json as _json
    from datetime import timezone as _tz

    class _NoIB:                                       # the bot never places orders in shadow
        def positions(self): return []
        def accountValues(self): return []
        def sleep(self, *_): pass

    if a.base <= 0:                                    # offline: no account to size against
        a.base = 100_000.0                             # nominal, so share counts are readable

    for sym in syms:
        # the replay must read bars on the SAME clock the bot runs (1-min since
        # the pivot). cache/ibkr5 is the legacy 5-min research cache — replaying it
        # through a 1-min Clock is incoherent, so use the pivot-era dataset.
        p = f"cache/ibkr1min_days/{sym}/{a.cache_day}.json"
        if not os.path.exists(p) and Clock5.WIDTH == 5:
            p = f"cache/ibkr5/{sym}/{a.cache_day}.json"   # legacy clock only
        if not os.path.exists(p):
            print(f"  ✗ no cached bars: {p}")
            continue
        rows = sorted(_json.load(open(p)), key=lambda r: r["t"])
        TightFlagTrader.RUN_MODE = "cache"     # offline regression, not a real run
        bot = TightFlagTrader(_NoIB(), None, None, a)
        bot.day = datetime.fromisoformat(a.cache_day).date()
        bot.log_path = bot._log_path_for(bot.day)      # not today's session record
        # previous session's close for the long gate — the prior cached day, from
        # the SAME dataset the bars came from (stale cache/ibkr5 path fixed 2026-09-04)
        cdir = os.path.dirname(p)
        days = sorted(f[:-5] for f in os.listdir(cdir) if f.endswith(".json"))
        i = days.index(a.cache_day)
        if i > 0:
            prev = _json.load(open(f"{cdir}/{days[i-1]}.json"))
            if prev:
                bot.prev_close[sym] = sorted(prev, key=lambda r: r["t"])[-1]["c"]
        print(f"\n  CACHE REPLAY {sym} {a.cache_day}   prev close "
              f"${bot.prev_close.get(sym, float('nan')):.2f}")
        seen = {}
        for r in rows:
            dt = datetime.fromtimestamp(r["t"]/1000, tz=_tz.utc).astimezone(ET).replace(tzinfo=None)
            mod = dt.hour*60 + dt.minute
            if mod < 570 or mod >= 960:
                continue
            k = (mod - 570)//Clock5.WIDTH
            if k in seen:                              # same window -> merge (as the scanner does)
                b = seen[k]
                b["h"] = max(b["h"], r["h"]); b["l"] = min(b["l"], r["l"])
                b["c"] = r["c"]; b["v"] += r["v"]
                continue
            seen[k] = dict(ts=dt, o=r["o"], h=r["h"], l=r["l"], c=r["c"], v=r["v"])
        for k in sorted(seen):
            b = seen[k]
            bot.on_5min(sym, k, b["ts"], b["o"], b["h"], b["l"], b["c"], b["v"], Clock5.WIDTH)
        bot.eod(datetime.combine(bot.day, EOD))


def main():
    ap = argparse.ArgumentParser(description="Tight-flag order robot (IBKR paper, shadow by default)")
    # accept BOTH styles:  "QQQ,NVDA,BA"   and   QQQ NVDA BA
    ap.add_argument("symbols", nargs="*", default=[],
                    help="tickers, space- or comma-separated. Default: the day's TradingView "
                         "export via the shared auto-discovery (~/Downloads/*DayTrade*.txt, "
                         "newest wins), same as the cup bot.")
    ap.add_argument("--watchlist", default="auto",
                    help="watchlist file path, or 'auto' (default) for the newest export")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002, help="Gateway paper 4002 | TWS paper 7497")
    ap.add_argument("--client-id", type=int, default=18,
                    help="Daily-program assignment: 18 = HTF live (cup live=8, cup replay=9, "
                         "HTF record/replay=19, HTF cache=20, cup tools=51-53). Two clients "
                         "on the same id knock each other off.")
    ap.add_argument("--arm", action="store_true", help="place PAPER orders (default: shadow)")
    ap.add_argument("--risk", type=float, default=0.0025,
                    help="risk fraction of equity per trade (default 0.25%%: this strategy's "
                         "stops are ~0.12%% of price, so 1%% risk implies ~8.5x equity notional)")
    ap.add_argument("--base", type=float, default=0.0, help="virtual sizing equity")
    ap.add_argument("--max-positions", type=int, default=2, help="cap on THIS bot's own positions")
    ap.add_argument("--max-notional", type=float, default=1.0,
                    help="hard cap on position notional as a MULTIPLE of equity "
                         "(default 1.0 = never exceed account size)")
    ap.add_argument("--delayed", action="store_true",
                    help="free 15-min delayed feed — PLUMBING ONLY, entries are ~15min late")
    ap.add_argument("--replay", action="store_true", help="walk the last completed session (shadow)")
    ap.add_argument("--trades", action="store_true",
                    help="print the trade ledger (deduped, one row per symbol-session) and exit")
    ap.add_argument("--mode-filter", default="",
                    help="with --trades: only show these run types, e.g. shadow,armed")
    ap.add_argument("--since", default="", help="with --trades: only sessions on/after YYYY-MM-DD")
    ap.add_argument("--cache-day", default="",
                    help="FIDELITY TEST: replay YYYY-MM-DD from cache/ibkr5 — the exact bars the "
                         "backtest scored — and print what this bot would have done, so the live "
                         "decision path can be diffed against data/events_tightflag_ibkr.jsonl. "
                         "Offline: needs no IBKR connection.")
    ap.add_argument("--i-understand-delayed", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--i-understand-live", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.port in LIVE_PORTS and not a.i_understand_live:
        sys.exit(f"✗ port {a.port} is a LIVE trading port — this robot is PAPER-only. Refusing.")
    if a.replay and a.arm:
        sys.exit("✗ --replay with --arm makes no sense. Replay is shadow-only.")
    if a.arm and a.delayed and not a.i_understand_delayed:
        sys.exit("✗ --arm with --delayed would enter ~15 minutes after the real signal — that is a\n"
                 "  different trade than the one backtested. Use the real-time feed to arm.")

    raw = a.symbols if isinstance(a.symbols, list) else [a.symbols]
    cli = ",".join(str(chunk) for chunk in raw) if raw else None
    syms, wl_where = read_watchlist(cli, a.watchlist)
    print(f"  symbols from: {wl_where}")
    if not syms:
        syms = list(DEFAULT_SYMS)

    if a.trades:
        show_trades(a)
        return

    if a.cache_day:
        cache_replay(a, syms)
        return

    try:
        from ib_async import IB, Stock, Order, MarketOrder
    except ImportError:
        sys.exit("✗ ib_async not installed.  ->  pip install ib_async")

    ib = IB()
    try:
        ib.connect(a.host, a.port, clientId=a.client_id, timeout=10)
    except Exception as e:
        sys.exit(f"✗ can't reach IB Gateway/TWS at {a.host}:{a.port}  ({e})\n"
                 f"  If the cup robot is already running, make sure --client-id differs.")
    ib.reqMarketDataType(3 if a.delayed else 1)
    ib.sleep(1)

    TightFlagTrader.RUN_MODE = ("armed" if a.arm else ("replay" if a.replay else "shadow"))
    bot = TightFlagTrader(ib, Order, MarketOrder, a)
    mode = "🔴 ARMED — placing PAPER orders" if a.arm else "🟢 SHADOW — logging only"
    print(f"TIGHT-FLAG ROBOT — IBKR paper — {mode}")
    print(f"  account {ib.managedAccounts()}  clientId {a.client_id}  sizing equity ${bot.equity():,.0f}")
    print(f"  data: {'DELAYED (plumbing only)' if a.delayed else 'real-time'} | "
          f"risk {a.risk*100:.1f}% | own-position cap {a.max_positions}")
    print(f"  TRADE LOG -> {os.path.abspath(LEDGER_CSV)}")
    print(f"  fills     -> {FILLS_CSV} | orderRef prefix {REF_PREFIX}-")
    print(f"  isolation: never global-cancels, never touches positions it did not open")
    print(f"  watching {', '.join(syms)}\n")

    # SMART alone is ambiguous for some ETFs on this gateway; pin the listing venue
    # (same mapping the history puller uses) and fall back to plain SMART.
    PRIMARY = {"QQQ": "NASDAQ", "SPY": "ARCA"}
    unknown = []
    for s in syms:
        c = Stock(s, "SMART", "USD")
        if s in PRIMARY:
            c.primaryExchange = PRIMARY[s]
        for attempt in (c, Stock(s, PRIMARY.get(s, "SMART"), "USD")):
            try:
                ib.qualifyContracts(attempt)
            except Exception:
                pass
            if getattr(attempt, "conId", 0):
                bot.contracts[s] = attempt
                break
        if s not in bot.contracts:                     # delisted / wrong ticker / no permission
            unknown.append(s)
    if unknown:
        print(f"  ⚠️ skipping unrecognised ticker(s): {', '.join(unknown)}  "
              f"(delisted, renamed, or not available on this account)")
        syms = [s for s in syms if s in bot.contracts]

    # USER 2026-09-03 universe policy: SPY and QQQ are the only tradable ETFs — every
    # other ETF/ETN/fund is dropped here, mirroring the cup screen's stockType rule so
    # the live session and the official record agree on the population.
    ETF_ALLOWED = {"SPY", "QQQ"}
    not_common = []
    for s2 in list(bot.contracts):
        if s2 in ETF_ALLOWED:
            continue
        try:
            cds = ib.reqContractDetails(bot.contracts[s2])
            st2 = (getattr(cds[0], "stockType", "") or "").upper() if cds else ""
        except Exception:
            st2 = ""
        if st2 and st2 not in ("COMMON", "ADR"):
            not_common.append((s2, st2))
            del bot.contracts[s2]
    if not_common:
        print("  🚫 not common stock (only SPY/QQQ may be ETFs): "
              + ", ".join(f"{a_}({b_})" for a_, b_ in not_common))
        syms = [s for s in syms if s in bot.contracts]
    if not syms:
        sys.exit("✗ none of the requested tickers could be resolved — nothing to do.")

    if a.arm and not a.replay:
        # STARTUP RECONCILIATION (2026-09-09): a previous run's TF- orders are
        # landmines (KLAC: an orphaned 228-share buy-stop). AUDIT FIX 2026-09-10:
        # cancel ONLY the legs that are safe to cancel. An unfilled ENTRY (-E) or a
        # stranded close (-C) is a landmine; a PROTECTIVE stop (-P) is the only thing
        # standing between a surviving position and an unlimited loss — shutdown_report
        # deliberately leaves those working, so sweeping them was un-doing our own
        # safety. Orders with no leg tag predate this fix and cannot be classified
        # (a short's entry is a SELL STP exactly like a long's protective stop), so
        # we refuse to arm rather than guess with real shares.
        try:
            stale = [t for t in ib.openTrades()
                     if str(getattr(t.order, "orderRef", "") or "").startswith(f"{REF_PREFIX}-")]
        except Exception:
            stale = []
        unknown = [t for t in stale
                   if not str(t.order.orderRef).rsplit("-", 1)[-1] in ("E", "P", "C")]
        if unknown:
            for t in unknown:
                print(f"  ⛔ working order {t.order.orderRef} ({t.order.action} "
                      f"{t.order.orderType} {t.order.totalQuantity:g} "
                      f"{t.contract.symbol}) carries no leg tag — it could be an unfilled "
                      f"entry OR a protective stop holding a live position.")
            sys.exit("✗ refusing to arm with unclassifiable TF- orders working. Cancel or "
                     "resolve them at IBKR (check positions first), then start again.")
        for t in stale:
            leg = str(t.order.orderRef).rsplit("-", 1)[-1]
            if leg == "P":
                print(f"  🛡️ LEAVING a protective stop working: {t.order.action} "
                      f"{t.order.totalQuantity:g} {t.contract.symbol} "
                      f"({t.order.orderRef}) — it is guarding a position from an earlier "
                      f"run. This bot will NOT manage that trade; flatten it at IBKR.")
                continue
            print(f"  🧹 cancelling a working {t.order.action} {t.order.orderType} "
                  f"{t.order.totalQuantity:g} {t.contract.symbol} from a previous run "
                  f"({t.order.orderRef}) — if IBKR shows a matching POSITION, flatten "
                  f"it manually before trusting today's run")
            try:
                ib.cancelOrder(t.order)
            except Exception:
                pass

    def sink_for(sym):
        def sink(k, ts, o, h, l, c, v, n):
            bot.on_5min(sym, k, ts, o, h, l, c, v, n)
        return sink

    # seed: 5 sessions of 1-min bars -> previous close + (in replay) the whole day
    _tallies = []
    for s in syms:
        bl = ib.reqHistoricalData(bot.contracts[s], endDateTime="", durationStr="5 D",
                                  barSizeSetting="1 min", whatToShow="TRADES", useRTH=True,
                                  formatDate=2, keepUpToDate=not a.replay)
        rows = [(x.date.astimezone(ET).replace(tzinfo=None) if hasattr(x.date, "astimezone") else x.date,
                 x.open, x.high, x.low, x.close, float(x.volume)) for x in bl]
        if not rows:
            print(f"  ⚠️ {s}: no history returned"); continue
        days = sorted({r[0].date() for r in rows})
        target = days[-1]
        if len(days) >= 2:                              # yesterday's close for the long gate
            prev_rows = [r for r in rows if r[0].date() == days[-2]]
            if prev_rows:
                bot.prev_close[s] = prev_rows[-1][4]
        bot.day = target
        bot.aggs[s] = Clock5(sink_for(s))
        if a.replay:
            print(f"  REPLAY {s} — session {target}  (prev close "
                  f"${bot.prev_close.get(s, float('nan')):.2f})")
            todays = [r for r in rows if r[0].date() == target]
            for (t, o, h, l, c, v) in todays:
                bot.aggs[s].add(t, o, h, l, c, v)
            bot.aggs[s].close_day()
            bot.eod(datetime.combine(target, EOD))
            _tallies.extend(bot.session_tally); bot.session_tally = []
        else:
            todays = [r for r in rows if r[0].date() == target]
            for (t, o, h, l, c, v) in todays:
                bot.aggs[s].add(t, o, h, l, c, v)
            print(f"  {s}: seeded {len(todays)} 1-min bars for {target} | prev close "
                  f"${bot.prev_close.get(s, float('nan')):.2f}")

    if a.replay:
        bot.session_tally = _tallies
        bot.run_summary()
        ib.disconnect()
        return

    entered: set = set()

    def on_update(bars, has_new_bar):
        if not has_new_bar or not len(bars):
            return
        sym = bars.contract.symbol
        x = bars[-2] if len(bars) >= 2 else bars[-1]    # last CLOSED 1-min bar
        t = x.date.astimezone(ET).replace(tzinfo=None) if hasattr(x.date, "astimezone") else x.date
        bot.day = t.date()
        bot.aggs[sym].add(t, x.open, x.high, x.low, x.close, float(x.volume))

    for s in syms:
        bl = ib.reqHistoricalData(bot.contracts[s], endDateTime="", durationStr="1 D",
                                  barSizeSetting="1 min", whatToShow="TRADES", useRTH=True,
                                  formatDate=2, keepUpToDate=True)
        bl.updateEvent += on_update

    print("  streaming… (Ctrl-C to stop; the bot flattens ONLY its own positions at 15:49)\n")
    try:
        while True:
            ib.sleep(5)
            now = datetime.now(ET).replace(tzinfo=None)
            # BLOCKER FIX 2026-07-28: the 15:45 bar does not flush until the 15:50 bar
            # arrives (~15:51), so a wall-clock 15:49 trigger booked off the 15:40 bar —
            # a different price from the one the backtest (and the fidelity replay) uses.
            # Wait for the real bar; only fall back on the clock well after it should
            # have arrived, so a dead feed still cannot strand a position overnight.
            if now.time() >= dtime(15, 52) and any(st.get("open") for st in bot.state.values()):
                bot.say("  ⏰ 15:52 and the 15:45 bar never arrived — flattening on the clock")
                bot.eod(now)
    except KeyboardInterrupt:
        print("\n  interrupted — writing ledger")
        if a.arm:
            bot.shutdown_report()
        bot.write_ledger()
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
