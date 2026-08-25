"""
claude_research.py — CUP COFFEE: the Claude-in-the-loop routine functions
=========================================================================
Two functions wrap the Anthropic API and are the ONLY points where a language
model touches the research loop:

    propose_factors()   -> Stage 5 hypothesis generation        (loop step 1)
    interpret_decay()   -> decay triage + replacement ideas     (loop step 7)

Everything between them — feature compute, IC scoring, the validation gates —
is deterministic code with NO model in the loop. That separation is the whole
point: Claude generates and interprets; math decides.

THE 0.60: it is the pattern's backtested BASELINE WIN RATE, not a trade filter.
It is the benchmark a factor must beat. A factor is useful only if it shifts
the CONDITIONAL win rate significantly off 0.60 — up (size in) or down (avoid).

Run `python claude_research.py` to PRINT the exact prompt that would be sent
(no API call, no key needed) so you can inspect and tune it.
"""

from __future__ import annotations
# this file lives in a subfolder — put the project root on sys.path and work from it
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT); _os.chdir(_ROOT)

import json
from dataclasses import dataclass
from typing import Optional

MODEL = "claude-opus-4-8"          # hypothesis generation wants the strongest model; swap as needed
BASELINE_WIN_RATE = 0.60


# ----------------------------------------------------------------------------
# What the loop passes IN to Claude
# ----------------------------------------------------------------------------

@dataclass
class FactorRecord:
    """An already-validated live factor and its out-of-sample performance."""
    name: str
    tier: str                  # tier0_regime | tier1_setup | tier2_microstructure | intratrade
    oos_ic: float              # out-of-sample rank IC
    winrate_lift: float        # conditional win rate MINUS the 0.60 baseline (the thing that matters)
    max_corr_to_existing: float


# ----------------------------------------------------------------------------
# Prompt construction — pure & testable (no API, inspect freely)
# ----------------------------------------------------------------------------

SYSTEM_PROPOSE = (
    "You are a senior quant generating factor hypotheses for an intraday "
    "cup-and-handle strategy on US equities (1/2/5-min charts; gap-catalyst "
    f"universe). The pattern's backtested baseline win rate is {BASELINE_WIN_RATE:.2f}. "
    "Your job: propose NEW factors that plausibly shift the CONDITIONAL win rate "
    f"meaningfully away from {BASELINE_WIN_RATE:.2f} — either higher-probability "
    "subsets to size into, or low-probability subsets to avoid or exit early. "
    "Every factor MUST have a concrete economic mechanism; a factor without a "
    "causal story is worthless — do not propose it. Prefer factors with low "
    "correlation to the existing set. Respond with JSON ONLY: no prose, no fences."
)

FACTOR_SCHEMA = """\
Return a JSON array; each element:
{
  "name": "snake_case_name",
  "tier": "tier0_regime | tier1_setup | tier2_microstructure | intratrade",
  "hypothesis": "one sentence: what it measures and the expected effect on win rate",
  "economic_mechanism": "the causal WHY this moves the win rate off baseline",
  "computation": "how to compute from OHLCV / level-2 / fundamentals / regime data",
  "expected_direction": "higher_value_raises_winrate | higher_value_lowers_winrate"
}"""


def build_proposal_prompt(existing: list[FactorRecord], n: int,
                          tier_focus: Optional[str] = None,
                          exit_timing: bool = True) -> str:
    lines = [f"BASELINE WIN RATE: {BASELINE_WIN_RATE:.2f}  (the benchmark every factor must beat)\n",
             "EXISTING LIVE FACTORS (out-of-sample performance):"]
    for f in existing:
        lines.append(
            f"  - {f.name} [{f.tier}]  OOS_IC={f.oos_ic:+.3f}  "
            f"winrate_lift={f.winrate_lift:+.3f}  max_corr_to_others={f.max_corr_to_existing:.2f}"
        )
    if tier_focus:
        lines.append(f"\nFOCUS TIER: {tier_focus}")
    if exit_timing:
        lines.append(
            "\nPRIORITY TARGET — EXIT TIMING: the highest-value factors are INTRATRADE "
            "signals that, mid-trade, predict whether holding past 60 minutes beats "
            "taking the 1-hour profit (or predict a fizzle to cut at breakeven). "
            "Weight proposals toward these."
        )
    lines.append(f"\nPropose {n} NEW factors, each low-correlation to the existing set.\n")
    lines.append(FACTOR_SCHEMA)
    return "\n".join(lines)


def _extract_json(text: str):
    """Strip optional markdown fences and parse JSON."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lower().startswith("json"):
            t = t[4:]
    return json.loads(t.strip())


# ----------------------------------------------------------------------------
# The two routine functions that call the API
# ----------------------------------------------------------------------------

def propose_factors(existing: list[FactorRecord], n: int = 5,
                    tier_focus: Optional[str] = None) -> list[dict]:
    """LOOP STEP 1. Ask Claude for n new factor hypotheses. Returns parsed list.
    Requires:  pip install anthropic   and   ANTHROPIC_API_KEY in the env."""
    from anthropic import Anthropic
    client = Anthropic()
    prompt = build_proposal_prompt(existing, n, tier_focus)
    resp = client.messages.create(
        model=MODEL, max_tokens=2000, system=SYSTEM_PROPOSE,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return _extract_json(text)


def interpret_decay(ic_history: dict[str, list[float]]) -> list[dict]:
    """LOOP STEP 7. Given the recent IC trail of factors flagged as decayed,
    ask Claude for a likely cause and a concrete refinement/replacement each.
    The decay DETECTION is done by code; Claude only interprets."""
    from anthropic import Anthropic
    client = Anthropic()
    body = ["These live factors decayed below the IC threshold. For each, give a "
            "likely CAUSE (crowding, regime shift, data artifact, overfit) and a "
            "concrete refinement or replacement.\n",
            "Return JSON array: {name, likely_cause, action}.\n"]
    for name, trail in ic_history.items():
        body.append(f"  - {name}: recent IC trail {trail}")
    resp = client.messages.create(
        model=MODEL, max_tokens=1500,
        system="You are a senior quant triaging decayed factors. JSON only, no fences.",
        messages=[{"role": "user", "content": "\n".join(body)}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return _extract_json(text)


# ----------------------------------------------------------------------------
# Inspect the prompt with no API call / no key
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    demo = [
        FactorRecord("breakout_volume_ratio", "tier1_setup",        0.082, 0.11, 0.20),
        FactorRecord("handle_volume_dryup",   "tier1_setup",        0.061, 0.08, 0.35),
        FactorRecord("right_rim_recovery",    "tier1_setup",        0.054, 0.07, 0.28),
    ]
    print("=" * 72)
    print("SYSTEM PROMPT\n")
    print(SYSTEM_PROPOSE)
    print("=" * 72)
    print("USER PROMPT (tier_focus='intratrade')\n")
    print(build_proposal_prompt(demo, n=5, tier_focus="intratrade"))
