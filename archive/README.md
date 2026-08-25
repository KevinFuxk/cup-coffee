# archive/ — retired code, kept on purpose

**What archiving means here:** nothing in this project gets deleted. Code that is replaced,
already applied, or broken moves into this folder — out of the import path and out of the way,
so it can never run by accident, but preserved so it can be read, compared against, or
restored. Deleting loses information; archiving loses nothing.

**To restore any file:** `git mv archive/superseded/<file> <where it came from>` (tracked
files keep their full history), or plain `mv` for the untracked ones. Restored research/tools
scripts also need the 4-line project-root shim the live folders use — copy it from any file
in `research/`.

**Rule:** nothing in here is ever imported or executed. If you need behavior from an archived
file, port it forward into the live tree — don't run it from here.

## superseded/ — replaced or already-applied code

| File | Archived | Why it's here | Replaced by |
|---|---|---|---|
| `live_signals.py` | 2026-08-25 | Alpaca-stream signals-only dry-run — the precursor of the order robot. Kept because the memory note "flip `--feed sip` later" refers to it if the Alpaca path is ever revived | `live_trader_ibkr.py` (signals + orders + shadow + replay) |
| `live_signals_ibkr.py` | 2026-08-25 | same idea on IBKR data, still signals-only | `live_trader_ibkr.py` |
| `run_today.py` | 2026-08-25 | one-shot "do my keys work" smoke test of the live chain | `daily_update.py` |
| `run_extend.py` | 2026-08-25 | one-time launcher that backfilled the 2021-06→2022-06 slice; that run is done and its events are in the piles | `run_history_v2.py` for full rebuilds |
| `enrich_cup_strictness.py` | 2026-08-25 | tagged loose-rim trades that would also pass STRICT symmetry — an approximation | the real strict re-run pile (`research/backfill.py` with `rim_symmetry: "min"` → `data/events_strict_cup.jsonl`) |
| `patch_strict_gap.py` | 2026-08-25 | one-shot patch for the strict pile's missing 2023-11→2024-06 window; applied in commit 94e4642 | nothing — its output is in the data |
| `test_detector.py` | 2026-08-25 | **broken, not just old**: imports `atr` from the detector, deleted in the v2 "pure geometry" rewrite — it cannot even be imported. Kept as a reference for test ideas | `tests/test_cup_coffee.py` (14 cases against the CURRENT rules) |
| `live_trader_ibkr.py.bak-20260727-221604` | 2026-08-25 | pre-2026-07-27 snapshot of the order robot (before own-book accounting) | the live `live_trader_ibkr.py` |

## junk/ — shell accidents (safe to delete, parked per the back-up-first rule)

| File | What it actually is |
|---|---|
| `MOMENTUM` | a captured Python SyntaxError traceback, redirected into a file by a broken heredoc |
| `consolidation_PY` | an empty heredoc fragment whose original filename contained a literal newline |

## old_runs/ — predates this cleanup

Outputs of earlier backfill runs, kept as-is.
