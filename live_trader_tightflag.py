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
from datetime import datetime, time as dtime
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
# The frozen labeler exits at the close of the LAST 5-min window that STARTS at or
# before 15:49 — i.e. the 15:45-15:50 bar. Booking on the next bar instead (15:50)
# prices the flat off post-15:50 trade and silently disagrees with the backtest;
# the bulk fidelity replay caught exactly that on 10 of 129 QQQ sessions.
EOD_BAR_START = dtime(15, 45)
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
        self.log_path = f"logs/tightflag_{datetime.now(ET):%Y-%m-%d}.log"

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
            self.say(f"  ⚠️ {sym} our STOP was cancelled by something outside this bot "
                     f"(global cancel / kill-switch / manual). Position is unprotected — "
                     f"booking at last price and standing down.")
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
        return f"{REF_PREFIX}-{sym}-{self.day:%Y%m%d}"

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
                       orderRef=ref, transmit=True)
        tr = self.ib.placeOrder(c, o)
        tr.fillEvent += self._fill_logger("ENTRY", sym)
        self.my_orders.setdefault(ref, {})["entry"] = tr
        self.say(f"  📌 {sym} resting {act}-STOP {qty} @ ${level:.2f} placed")
        return tr

    def attach_protective_stop(self, sym, side, qty, stop):
        """Protective STP for a position that has just filled."""
        c = self.contracts[sym]
        ref = self._ref(sym)
        exit_act = "SELL" if side == "long" else "BUY"
        o = self.Order(orderId=self.ib.client.getReqId(), action=exit_act, orderType="STP",
                       totalQuantity=qty, auxPrice=round(stop, 2), tif="DAY",
                       orderRef=ref, transmit=True)
        tr = self.ib.placeOrder(c, o)
        tr.fillEvent += self._fill_logger("STOP", sym)
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
        h = self.my_orders.get(self._ref(sym)) or {}
        tr = h.get("stop")
        stt = getattr(getattr(tr, "orderStatus", None), "status", "") if tr else "missing"
        if tr is None or stt in ("Inactive", "ApiCancelled", "Cancelled"):
            self.say(f"  🚨 {sym} protective stop is {stt} while the position is OPEN — "
                     f"closing at market now (never leave it naked)")
            self.close_my_position(sym, st, f"stop {stt}")
            return False
        return True

    def move_stop(self, sym, new_stop):
        ref = self._ref(sym)
        h = self.my_orders.get(ref)
        if not h:
            return
        o = h["stop"].order
        o.auxPrice = round(new_stop, 2)
        self.ib.placeOrder(self.contracts[sym], o)     # same orderId = modify

    def cancel_my_stop(self, sym):
        h = self.my_orders.get(self._ref(sym))
        if h and h.get("stop"):
            try:
                self.ib.cancelOrder(h["stop"].order)
            except Exception:
                pass

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
        want = int(st.get("qty") or 0)
        if not (self.a.arm and want > 0):
            self.say(f"  ⛔ {sym} closing own position ({why})")
            return
        net = self.broker_qty(sym)
        ours_long = st["side"] == "long"
        same_side = (net > 0) if ours_long else (net < 0)
        if not same_side:
            self.say(f"  ⚠️ {sym} NOT sending a close ({why}) — broker shows {net:+.0f}, "
                     f"which is flat or opposite to our {'long' if ours_long else 'short'}. "
                     f"Something already closed us; sending an order would OPEN a reverse position.")
            return
        qty = int(min(want, abs(net)))
        act = "SELL" if ours_long else "BUY"
        o = self.MarketOrder(act, qty)
        o.orderRef = self._ref(sym)
        self.ib.placeOrder(self.contracts[sym], o)
        extra = "" if qty == want else f"  (clamped from {want}; broker net {net:+.0f})"
        self.say(f"  ⛔ {sym} closing OUR {qty} ({why}){extra}")

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
        self.log_path = f"logs/tightflag_{day:%Y-%m-%d}.log"

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

        # ---- resolve the resting STOP-ENTRY (bar 3 .. 09:45, USER 2026-09-02) ----
        _w = Clock5.WIDTH
        _dead = self.cfg.get("entry_deadline_min", 15)
        if (not st.get("open") and k >= self.cfg.get("trigger_bar", 2)
                and k * _w < _dead):
            lvl, lng = st["entry_level"], st["side"] == "long"
            if lng:
                px = o if o >= lvl else (lvl if h >= lvl else None)
            else:
                px = o if o <= lvl else (lvl if l <= lvl else None)
            if px is None:
                if (k + 1) * _w >= _dead:              # that was the last eligible bucket
                    self.done[sym] = True
                    self.state.pop(sym, None)
                    self.cancel_my_stop(sym)           # pull the unfilled entry order
                    self.say(f"  · {sym} no trade — never reached ${lvl:.2f} by 09:45 (no_trigger)")
                return                                 # else: keep resting into the next bar
            self._open_trade(sym, st, px, ts)
            if st.get("open"):
                st["fill_k"] = k       # same-bar stop-outs price at the stop level
            # fall through: this same bar 3 is also managed (stop-first convention)

        if not st.get("open"):
            return

        # ---- manage an open trade ----
        if self.check_external_close(sym, st, ts, c):
            return
        if not self.assert_protected(sym, st, ts):
            self._book(sym, st, c, "EXT", ts)
            return
        lng = st["side"] == "long"
        R = st["R"]
        fav = ((h - st["entry"]) if lng else (st["entry"] - l)) / R
        st["mfe"] = max(st.get("mfe", 0.0), fav)
        hit = (l <= st["stop"]) if lng else (h >= st["stop"])
        if hit:
            if st.get("fill_k") == k:
                px = st["stop"]        # same-bar stop-out: the open predates our fill
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
        if ts.time() >= EOD_BAR_START:                 # the 15:45 bar just closed -> flat
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
        """The resting stop-entry filled during bar 3 at `price` (the level, or bar 3's
        open if the market gapped through the order)."""
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
            if kind == "EOD":
                self.close_my_position(sym, st, "EOD 15:49")
            else:
                # BLOCKER FIX 2026-07-28: a STOP/EXT booking used to just cancel and walk
                # away, assuming the broker's stop had filled. If it had been rejected or
                # sat Inactive, the position was still live, unprotected and unmanaged.
                # Verify flat; if not, close what is actually there.
                net = self.broker_qty(sym)
                ours_long = st["side"] == "long"
                still_open = (net > 0) if ours_long else (net < 0)
                if still_open:
                    self.say(f"  🚨 {sym} booked {kind} but the broker still shows {net:+.0f} — "
                             f"the protective stop did NOT fill. Closing at market.")
                    st["qty"] = int(min(st.get("qty") or 0, abs(net))) or int(abs(net))
                    self.close_my_position(sym, st, f"{kind} not filled")
                else:
                    self.cancel_my_stop(sym)
        st["open"] = False
        self.done[sym] = True

    # ---- EOD -------------------------------------------------------------
    def eod(self, ts=None):
        ts = ts or datetime.now(ET).replace(tzinfo=None)
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
        # previous session's close for the long gate — the prior cached day
        days = sorted(f[:-5] for f in os.listdir(f"cache/ibkr5/{sym}") if f.endswith(".json"))
        i = days.index(a.cache_day)
        if i > 0:
            prev = _json.load(open(f"cache/ibkr5/{sym}/{days[i-1]}.json"))
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
    if not syms:
        sys.exit("✗ none of the requested tickers could be resolved — nothing to do.")

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
        bot.write_ledger()
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
