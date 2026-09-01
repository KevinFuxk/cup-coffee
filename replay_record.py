"""
replay_record.py — the DAILY TRADE LEDGER (user request 2026-07-22)
===================================================================
One command per evening (after ~16:20 ET, IB Gateway logged in):

    python replay_record.py

It replays the completed session TWICE — with the min-stop screen (0.25% of price) and
without — appends every trade to data/replay_trades.csv (opens directly in Excel/Numbers),
and regenerates data/replay_trades.html: a clean, filterable table of all recorded trades
with the basic statistics per variant (trades, wins, total R, avg R). Idempotent: re-running
the same day just refreshes that day's rows.
"""
import csv
import os
import subprocess
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
PY = sys.executable
LEDGER, HTML = "data/replay_trades.csv", "data/replay_trades.html"


def run_replays():
    base = [PY, "live_trader_ibkr.py", "--replay", "--tfs", "15s", "--port", "4002", "--client-id", "9"]
    for ms in ("0.25", "0"):
        print(f"=== replay with min-stop {ms}% ===", flush=True)
        r = subprocess.run(base + ["--minstop", ms], capture_output=True, text=True)
        lines = [l for l in r.stdout.splitlines() if "TRADES SUMMARY" in l or "ledger:" in l
                 or "session replayed" in l or l.strip().startswith(("closed", "0", "1", "2"))]
        for l in r.stdout.splitlines():
            if "session replayed" in l or "ledger:" in l:
                print("  " + l.strip())
        if r.returncode != 0:
            print(r.stderr[-800:])
            sys.exit("replay failed — is IB Gateway running & logged in (paper, port 4002)?")


def build_html():
    rows = list(csv.DictReader(open(LEDGER)))
    variants = sorted({r["variant"] for r in rows})
    sessions = sorted({r["session"] for r in rows}, reverse=True)

    def stats(rs):
        n = len(rs)
        tot = sum(float(r["R"]) for r in rs)
        wins = sum(1 for r in rs if float(r["R"]) > 0)
        kinds = defaultdict(int)
        for r in rs:
            kinds[r["exit_kind"]] += 1
        return (f"<b>{n}</b> trades over {len({r['session'] for r in rs})} day(s) · "
                f"<b>{wins}</b> wins ({wins / n * 100:.0f}%) · total <b>{tot:+.2f}R</b> · "
                f"avg {tot / n:+.3f}R/trade · exits: {dict(kinds)}") if n else "no trades yet"

    cards = "".join(
        f"<div class='card'><h3>{v}</h3><p>{stats([r for r in rows if r['variant'] == v])}</p></div>"
        for v in variants)

    # ---- universe join: sources, overlap-alpha, recorded drops (pivot 2026-09-02) ----
    upanel = ""
    if os.path.exists("data/universe_log.csv"):
        uni = list(csv.DictReader(open("data/universe_log.csv")))
        usrc = {(u["day"], u["symbol"]): u for u in uni}
        live = [r for r in rows if r["variant"] == "minstop=0.25"]
        grp = {"overlap (2+ sources)": [], "single source": [], "unlogged day": []}
        for r in live:
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
                    chips += (f"<span class='chip drop' title='{u['reason']}'>"
                              f"{lab} 🚫</span>")
            days_html += f"<p><b>{d}</b> &nbsp;{chips}</p>"
        upanel = (f"<h2>Overlap alpha (minstop=0.25)</h2><div class='cards'>{ostat}</div>"
                  f"<h2>Daily universe — every ticker, drops with reasons (hover)</h2>{days_html}")
    head = "".join(f"<th>{c}</th>" for c in
                   ["session", "variant", "symbol", "tf", "entry time", "entry", "trigger",
                    "stop", "stop %", "exit time", "exit", "@ price", "R", "peak R"])
    body = ""
    for r in sorted(rows, key=lambda x: (x["session"], x["variant"], x["exit_time"]), reverse=True):
        rr = float(r["R"])
        cls = "pos" if rr > 0 else "neg" if rr < 0 else ""
        body += (f"<tr data-v='{r['variant']}' data-s='{r['session']}'>"
                 f"<td>{r['session']}</td><td>{r['variant']}</td><td>{r['symbol']}</td>"
                 f"<td>{r['tf']}</td><td>{r['entry_time']}</td><td>{r['entry']}</td>"
                 f"<td>{r['trigger']}</td><td>{r['stop']}</td><td>{r['stop_pct']}</td>"
                 f"<td>{r['exit_time']}</td><td>{r['exit_kind']}</td><td>{r['exit']}</td>"
                 f"<td class='{cls}'>{rr:+.2f}</td><td>{r.get('peak_R', '')}</td></tr>")
    vopts = "".join(f"<option>{v}</option>" for v in variants)
    sopts = "".join(f"<option>{s}</option>" for s in sessions)
    html = f"""<!doctype html><meta charset="utf-8"><title>Cup&Handle — replay trade ledger</title>
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
<h1>Cup &amp; Handle — daily replay trade ledger</h1>
<div class="cards">{cards}</div>
filter: <select id="fv" onchange="F()"><option>all variants</option>{vopts}</select>
<select id="fs" onchange="F()"><option>all sessions</option>{sopts}</select>
{upanel}\n<h2>All recorded trades</h2>\n<table><thead><tr>{head}</tr></thead><tbody id="tb">{body}</tbody></table>
<script>
function F(){{const v=fv.value,s=fs.value;for(const r of tb.rows)
 r.style.display=((v.startsWith('all')||r.dataset.v===v)&&(s.startsWith('all')||r.dataset.s===s))?'':'none';}}
</script>"""
    open(HTML, "w").write(html)
    print(f"view -> {HTML}   (open with: open {HTML})")


def explain():
    """Why each watched symbol did or didn't trade — the gate that rejected each cup."""
    print("\n" + "=" * 70)
    r = subprocess.run([PY, "explain_day.py"], capture_output=True, text=True)
    out = "\n".join(l for l in r.stdout.splitlines() if l.strip())
    print(out if out else "  (explain_day.py produced no output — is IB Gateway running?)")


def cache_bars():
    """Accumulate the private 15s story-stock dataset — the data money can't buy."""
    print("=" * 70)
    r = subprocess.run([PY, "cache_15s.py"], capture_output=True, text=True)
    for l in r.stdout.splitlines()[-6:]:
        print(l)


if __name__ == "__main__":
    run_replays()
    build_html()
    cache_bars()
    explain()
