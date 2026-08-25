"""
splits.py — Develop / Validate / Lockbox data discipline
=========================================================
Phase 0 infrastructure. Physically separates a dataset into three slices AND enforces
the access rules in code, because a folder you can read is not a lockbox.

    THE RULES
      Develop   — tweak freely, unlimited access
      Validate  — you may look TWICE, ever. Every access is counted and logged.
      Lockbox   — you may open it ONCE, at the very end, and you must say why.

Build the slices (one time):
    python splits.py --build spy_qqq

Use them in research:
    from splits import load_slice, sessions
    for sym, day in sessions("spy_qqq", "develop"):
        bars = load_slice("spy_qqq", "develop", sym, day)

Opening sealed data requires an explicit, logged reason — you cannot do it by accident:
    sessions("spy_qqq", "lockbox", unlock="final test of frozen v3 rules, 2026-08-01")

Every access to validate/lockbox is appended to data/splits/ACCESS_LOG.txt with a
timestamp and reason. That log is the honest record of how many times you peeked.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime

BASE = "data/splits"
LOG = f"{BASE}/ACCESS_LOG.txt"

# dataset -> (source dir builder, slice date ranges)
DATASETS = {
    "spy_qqq": {
        "source": "cache/ibkr5",
        "symbols": ("SPY", "QQQ"),
        "slices": {"develop": ("2004", "2016"),
                   "validate": ("2017", "2021"),
                   "lockbox": ("2022", "2026")},
        "note": "IBKR SPY/QQQ 5-minute RTH bars",
    },
}
FREE = "develop"
LIMITS = {"validate": 2, "lockbox": 1}


# ---------------------------------------------------------------- build
def build(name: str):
    ds = DATASETS[name]
    src = ds["source"]
    made = {}
    for slice_name, (lo, hi) in ds["slices"].items():
        n = 0
        for sym in ds["symbols"]:
            out = f"{BASE}/{name}/{slice_name}/{sym}"
            os.makedirs(out, exist_ok=True)
            for fn in sorted(os.listdir(f"{src}/{sym}")):
                if not fn.endswith(".json"):
                    continue
                if lo <= fn[:4] <= hi:
                    shutil.copy2(f"{src}/{sym}/{fn}", f"{out}/{fn}")
                    n += 1
        made[slice_name] = n
        print(f"  {slice_name:9} {lo}-{hi}: {n:5} session files")

    manifest = {"dataset": name, "note": ds["note"], "built": datetime.now().isoformat(timespec="seconds"),
                "slices": {k: {"range": ds["slices"][k], "files": v} for k, v in made.items()},
                "rules": {"develop": "unlimited", "validate": "2 looks ever", "lockbox": "1 look, at the end"},
                "warning": ("Decisions made BEFORE this split saw the whole dataset. "
                            "Validate/Lockbox are therefore clean only for rules frozen from the build date onward.")}
    os.makedirs(BASE, exist_ok=True)
    json.dump(manifest, open(f"{BASE}/{name}_manifest.json", "w"), indent=2)
    if not os.path.exists(LOG):
        open(LOG, "w").write("# every access to validate/lockbox is recorded here\n")
    print(f"\n  manifest -> {BASE}/{name}_manifest.json")
    print(f"  access log -> {LOG}")


# ---------------------------------------------------------------- access control
def _count(name: str, slice_name: str) -> int:
    if not os.path.exists(LOG):
        return 0
    tag = f"{name}/{slice_name}"
    return sum(1 for l in open(LOG) if l.startswith("ACCESS") and tag in l)


def _authorize(name: str, slice_name: str, unlock: str | None):
    if slice_name == FREE:
        return
    limit = LIMITS.get(slice_name)
    used = _count(name, slice_name)
    if unlock is None:
        raise PermissionError(
            f"\n  '{slice_name}' is sealed data ({used}/{limit} looks used).\n"
            f"  To open it you must state a reason:\n"
            f"      sessions('{name}', '{slice_name}', unlock='why you are opening it')\n"
            f"  This will be permanently recorded in {LOG}.")
    if used >= limit:
        raise PermissionError(
            f"\n  '{slice_name}' has already been opened {used}/{limit} times — the budget is spent.\n"
            f"  See {LOG}. Opening it again would make the result meaningless;\n"
            f"  gather new forward data instead.")
    with open(LOG, "a") as f:
        f.write(f"ACCESS {datetime.now().isoformat(timespec='seconds')}  {name}/{slice_name}  "
                f"(look {used+1} of {limit})  reason: {unlock}\n")
    print(f"⚠️  opened {name}/{slice_name} — look {used+1} of {limit}. Logged.", file=sys.stderr)


# ---------------------------------------------------------------- use
def sessions(name: str, slice_name: str, unlock: str | None = None) -> list[tuple[str, str]]:
    """[(symbol, 'YYYY-MM-DD'), ...] for one slice. Sealed slices need an unlock reason."""
    _authorize(name, slice_name, unlock)
    out = []
    for sym in DATASETS[name]["symbols"]:
        d = f"{BASE}/{name}/{slice_name}/{sym}"
        if os.path.isdir(d):
            out += [(sym, fn[:-5]) for fn in sorted(os.listdir(d)) if fn.endswith(".json")]
    return sorted(out, key=lambda x: (x[1], x[0]))


def load_slice(name: str, slice_name: str, symbol: str, day: str):
    """Raw rows for one session. Assumes you already called sessions() and were authorized."""
    p = f"{BASE}/{name}/{slice_name}/{symbol}/{day}.json"
    return json.load(open(p)) if os.path.exists(p) else None


def status(name: str):
    m = json.load(open(f"{BASE}/{name}_manifest.json"))
    print(f"{name} — {m['note']}   (built {m['built']})")
    for s, info in m["slices"].items():
        lo, hi = info["range"]
        used = _count(name, s)
        lim = LIMITS.get(s)
        budget = "unlimited" if lim is None else f"{used}/{lim} looks used"
        print(f"  {s:9} {lo}-{hi}  {info['files']:5} files   {budget}")
    print(f"\n  ⚠️ {m['warning']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", metavar="DATASET")
    ap.add_argument("--status", metavar="DATASET")
    a = ap.parse_args()
    if a.build:
        build(a.build)
    elif a.status:
        status(a.status)
    else:
        ap.print_help()
