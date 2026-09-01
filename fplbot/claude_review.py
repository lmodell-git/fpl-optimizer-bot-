"""Claude API sanity-check pass.

The MIP gives the mathematically optimal move for the numbers it was fed. It
does not know a player was subbed at half-time with a knock two hours ago, or
that a manager hinted at rotation in today's presser. Claude's job (SPEC.md
§"Where Claude fits"):

  * sanity-check the solver's output against very recent news;
  * flag close judgement calls the model can't quantify;
  * write the human-readable reasoning that goes in the email.

Uses the official Anthropic Python SDK. Model: claude-opus-5 with adaptive
thinking. Requires ANTHROPIC_API_KEY; if it's missing the pipeline still runs
and just ships the raw solver output (see run.py).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

_MODEL = os.environ.get("FPL_CLAUDE_MODEL", "claude-opus-5")


@dataclass
class Review:
    ok: bool
    verdict: str                 # "endorse" | "amend" | "hold" | "unavailable"
    summary: str                 # 2–4 sentence human summary for the email
    concerns: list[str]
    suggested_changes: list[str]
    raw: str = ""


_SYSTEM = """You are the judgement layer of an automated Fantasy Premier League bot.
A mixed-integer optimiser has already produced a mathematically optimal plan from
expected-points numbers. You do NOT redo the optimisation. You pressure-test it:

- Does any recommended transfer-in or captain have an unpriced red flag the
  numbers would miss? (late fitness test, suspension risk, benching hint,
  a striker just dropped for a cup game, imminent price/manager change.)
- Is a differential captain call sensible given the stated rank profile, or
  should it revert to template?
- Is taking a -4 hit justified here, or should the manager roll the transfer?

Be decisive and brief. Prefer endorsing a sound plan over inventing objections.
Respond ONLY with a JSON object matching the schema you are given."""

_SCHEMA_HINT = """{
  "verdict": "endorse | amend | hold",
  "summary": "2-4 sentences a human reads in an email",
  "concerns": ["short bullet", "..."],
  "suggested_changes": ["e.g. 'captain X over Y — Y flagged 75%'", "..."]
}"""


def _payload(context: dict) -> str:
    return (
        "PLAN AND CONTEXT (JSON):\n"
        + json.dumps(context, indent=2, default=str)
        + "\n\nReturn ONLY JSON with this shape:\n"
        + _SCHEMA_HINT
    )


def review_plan(context: dict, *, use_web_search: bool = True) -> Review:
    """Run the sanity-check. `context` is the dict built by report.build_context()."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return Review(False, "unavailable", "Claude review skipped — no ANTHROPIC_API_KEY set.",
                      [], [], raw="")
    try:
        import anthropic
    except ImportError:
        return Review(False, "unavailable", "Claude review skipped — `anthropic` not installed.",
                      [], [], raw="")

    client = anthropic.Anthropic()
    tools = []
    if use_web_search:
        # Latest fitness/team news the data pull can't have. Cheap, capped.
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}]

    try:
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=4000,
            thinking={"type": "adaptive"},
            system=_SYSTEM,
            tools=tools,
            messages=[{"role": "user", "content": _payload(context)}],
        )
    except Exception as exc:  # noqa: BLE001 — never let the review crash the run
        return Review(False, "unavailable", f"Claude review errored: {exc}", [], [], raw=str(exc))

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    parsed = _extract_json(text)
    if parsed is None:
        return Review(True, "endorse", text[:800] or "Claude returned no parseable verdict.",
                      [], [], raw=text)

    return Review(
        ok=True,
        verdict=str(parsed.get("verdict", "endorse")).lower().strip(),
        summary=str(parsed.get("summary", "")).strip(),
        concerns=[str(c) for c in parsed.get("concerns", []) if str(c).strip()],
        suggested_changes=[str(c) for c in parsed.get("suggested_changes", []) if str(c).strip()],
        raw=text,
    )


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1].removeprefix("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, depth = text.find("{"), 0
    if start == -1:
        return None
    for i in range(start, len(text)):
        depth += (text[i] == "{") - (text[i] == "}")
        if depth == 0:
            try:
                return json.loads(text[start:i + 1])
            except json.JSONDecodeError:
                return None
    return None
