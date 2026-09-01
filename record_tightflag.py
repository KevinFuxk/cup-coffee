"""
record_tightflag.py — the HTF channel's ONE evening command (after ~16:20 ET)
==============================================================================
Mirrors the cup channel's replay_record.py for the 1-minute high tight flag:

    python record_tightflag.py                # the latest archived watchlist day
    python record_tightflag.py 2026-08-28     # a specific day (backfill)

  1. caches every watched symbol's completed 1-min session to
     cache/ibkr1min_days/ (idempotent — cache_1min_days.py)
  2. replays the session through the FROZEN live HTF rules and appends the
     day's rows to data/replay_trades_tightflag.csv (14-column cup schema,
     variant "htf", idempotent per session — record_day_tightflag.py)
  3. regenerates data/replay_trades_tightflag.html — stats cards, the
     source-overlap alpha panel joined READ-ONLY from data/universe_log.csv,
     and the filterable all-trades table

Needs IB Gateway (paper, port 4002) logged in. Never touches the cup
channel's files: data/replay_trades.csv/.html, data/watchlists/,
data/universe_log.csv are read-only or untouched here.
"""
from __future__ import annotations

import csv
import glob
import os
import subprocess
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
PY = sys.executable
LEDGER = "data/replay_trades_tightflag.csv"
HTML = "data/replay_trades_tightflag.html"


def target_day() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    files = sorted(glob.glob("data/watchlists/*.txt"))
    if not files:
        sys.exit("✗ no archived watchlists in data/watchlists/ — run the morning bot first")
    return os.path.basename(files[-1])[:-4]


def run(cmd: list[str], label: str) -> None:
    print("=" * 70)
    print(f"=== {label}", flush=True)
    r = subprocess.run(cmd, text=True)
    if r.returncode != 0:
        sys.exit(f"✗ {label} failed — is IB Gateway running & logged in (paper, 4002)?")


def build_html() -> None:
    if not os.path.exists(LEDGER):
        print(f"(no ledger yet at {LEDGER} — nothing to render)")
        return
    rows = list(csv.DictReader(open(LEDGER)))
    sessions = sorted({r["session"] for r in rows}, reverse=True)

    def stats(rs):
        n = len(rs)
        if not n:
            return "no trades yet"
        tot = sum(float(r["R"]) for r in rs)
        wins = sum(1 for r in rs if float(r["R"]) > 0)
        kinds = defaultdict(int)
        for r in rs:
            kinds[r["exit_kind"]] += 1
        flies = sum(1 for r in rs if float(r["peak_R"] or 0) >= 1.75)
        return (f"<b>{n}</b> trades over {len({r['session'] for r in rs})} day(s) · "
                f"<b>{wins}</b> wins ({wins / n * 100:.0f}%) · total <b>{tot:+.2f}R</b> · "
                f"avg {tot / n:+.3f}R/trade · exits: {dict(kinds)} · "
                f"reached ≥1.75R: {flies}")

    cards = f"<div class='card'><h3>htf (all days)</h3><p>{stats(rows)}</p></div>"
    last = sessions[0] if sessions else ""
    if last:
        cards += (f"<div class='card'><h3>latest session {last}</h3>"
                  f"<p>{stats([r for r in rows if r['session'] == last])}</p></div>")

    # ---- universe join (READ-ONLY): overlap alpha + per-day drops ----
    upanel = ""
    if os.path.exists("data/universe_log.csv"):
        uni = list(csv.DictReader(open("data/universe_log.csv")))
        usrc = {(u["day"], u["symbol"]): u for u in uni}
        grp = {"overlap (2+ sources)": [], "single source": [], "unlogged day": []}
        for r in rows:
            u = usrc.get((r["session"], r["symbol"]))
            key = ("unlogged day" if u is None else
                   "overlap (2+ sources)" if int(u["n_sources"]) >= 2 else "single source")
            grp[key].append(float(r["R"]))
        ostat = ""
        for g, xs in grp.items():
            if xs:
                ostat += (f"<div class='card'><h3>{g}</h3><p><b>{len(xs)}</b> trades · "
                          f"total <b>{sum(xs):+.2f}R</b> · avg {sum(xs)/len(xs):+.3f}R/trade</p></div>")
        days_html = ""
        for d in sorted({u["day"] for u in uni}, reverse=True):
            chips = ""
            for u in [u for u in uni if u["day"] == d]:
                lab = f"{u['symbol']}·{u['n_sources']}src"
                if u["qualified"] == "yes":
                    cls = "chip hot" if int(u["n_sources"]) >= 2 else "chip"
                    chips += f"<span class='{cls}' title='{u['sources']}'>{lab}</span>"
                else:
                    chips += f"<span class='chip drop' title='{u['reason']}'>{lab} 🚫</span>"
            days_html += f"<p><b>{d}</b> &nbsp;{chips}</p>"
        upanel = (f"<h2>Overlap alpha (htf)</h2><div class='cards'>{ostat}</div>"
                  f"<h2>Daily universe — every ticker, drops with reasons (hover)</h2>"
                  f"<p style='color:#777;font-size:12px'>universe_log.csv is owned by the "
                  f"cup channel; joined here read-only.</p>{days_html}")

    head = "".join(f"<th>{c}</th>" for c in
                   ["session", "variant", "symbol", "tf", "entry time", "entry", "trigger",
                    "stop", "stop %", "exit time", "exit", "@ price", "R", "peak R"])
    body = ""
    for r in sorted(rows, key=lambda x: (x["session"], x["exit_time"]), reverse=True):
        rr = float(r["R"])
        cls = "pos" if rr > 0 else "neg" if rr < 0 else ""
        body += (f"<tr data-s='{r['session']}'>"
                 f"<td>{r['session']}</td><td>{r['variant']}</td><td>{r['symbol']}</td>"
                 f"<td>{r['tf']}</td><td>{r['entry_time']}</td><td>{r['entry']}</td>"
                 f"<td>{r['trigger']}</td><td>{r['stop']}</td><td>{r['stop_pct']}</td>"
                 f"<td>{r['exit_time']}</td><td>{r['exit_kind']}</td><td>{r['exit']}</td>"
                 f"<td class='{cls}'>{rr:+.2f}</td><td>{r.get('peak_R', '')}</td></tr>")
    sopts = "".join(f"<option>{s}</option>" for s in sessions)
    html = f"""<!doctype html><meta charset="utf-8"><title>High Tight Flag — replay trade ledger</title>
<style>
 body{{font:14px -apple-system,system-ui,sans-serif;margin:24px;color:#1a1a1a}}
 h1{{font-size:20px}} .cards{{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0 20px}}
 .card{{border:1px solid #ddd;border-radius:8px;padding:10px 14px;background:#fafafa}}
 .card h3{{margin:0 0 6px;font-size:14px}} .card p{{margin:0;font-size:13px}}
 select{{margin-right:12px;padding:4px}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 th,td{{border-bottom:1px solid #eee;padding:5px 8px;text-align:right}}
 th{{background:#f5f5f5;position:sticky;top:0}} td:nth-child(-n+5),th:nth-child(-n+5){{text-align:left}}
 .pos{{color:#0a7d33;font-weight:600}} .neg{{color:#c22;font-weight:600}}
 h2{{font-size:16px;margin:22px 0 8px}}
 .chip{{display:inline-block;border:1px solid #ccc;border-radius:10px;padding:1px 8px;
        margin:2px;font-size:12px;background:#fff}}
 .chip.hot{{border-color:#0a7d33;background:#eafbee;font-weight:600}}
 .chip.drop{{border-color:#c22;background:#fdeeee;color:#933;text-decoration:line-through}}
</style>
<h1>High Tight Flag — daily replay trade ledger (1-min, variant htf)</h1>
<div class="cards">{cards}</div>
filter: <select id="fs" onchange="F()"><option>all sessions</option>{sopts}</select>
{upanel}
<h2>All recorded trades</h2>
<table><thead><tr>{head}</tr></thead><tbody id="tb">{body}</tbody></table>
<script>
function F(){{const s=fs.value;for(const r of tb.rows)
 r.style.display=(s.startsWith('all')||r.dataset.s===s)?'':'none';}}
</script>"""
    open(HTML, "w").write(html)
    print(f"view -> {HTML}   (open with: open {HTML})")


if __name__ == "__main__":
    day = target_day()
    run([PY, "cache_1min_days.py", "--day", day], f"cache 1-min bars ({day})")
    run([PY, "record_day_tightflag.py", day], f"record the session ({day})")
    build_html()
