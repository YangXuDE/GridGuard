"""
GridGuard — DeepSeek LLM corrective control agent.

The agent receives:
  - current post-fault network state (overloads, voltages)
  - feasible action space (BESS / RES curtailment / load curtailment)
  - weather & market context
  - iteration history

It returns a structured dispatch decision (action_id + MW) per iteration.
Dispatch priority: BESS discharge → curtail renewables → shed load.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from environment import ActionOption, PostFaultState


# ── data classes ──────────────────────────────────────────────────────────────

@dataclass
class DispatchCommand:
    action_id: str
    amount_mw: float
    reasoning_snippet: str = ""


@dataclass
class CorrectiveIteration:
    iteration: int
    reasoning: str
    commands: list[DispatchCommand]
    max_loading_before: float
    max_loading_after: float
    overloads_before: int
    overloads_after: int
    undervolt_before: int
    undervolt_after: int
    cost_eur: float


# ── prompt builder ─────────────────────────────────────────────────────────────

def _fmt_state(state: PostFaultState) -> str:
    lines = [
        f"  Max line loading : {state.max_loading_pct:.1f}%",
        f"  Overloaded lines : {len(state.overloaded_lines)}",
        f"  Under-voltage buses: {state.undervolt_buses}",
    ]
    for ol in state.overloaded_lines[:5]:
        lines.append(f"    - {ol['name']} ({ol['from_bus']}→{ol['to_bus']}): {ol['loading_pct']:.1f}%")
    return "\n".join(lines)


def _fmt_actions(options: list[ActionOption]) -> str:
    lines = []
    for o in options:
        tag = " ★" if o.recommended else ""
        soc = f"  SoC {o.soc_pct}%" if o.soc_pct is not None else ""
        lines.append(
            f"  [{o.target_id}]{tag}  {o.label}"
            f"\n    type={o.action_type}  max={o.max_available_mw} MW"
            f"  cost={o.cost_per_mw:,.0f} €/MWh  sens={o.sensitivity:+.2f} pp/MW"
            f"  on {o.target_line}{soc}"
        )
    return "\n".join(lines)


SYSTEM_PROMPT = """You are GridGuard, an AI agent for power distribution grid security.

Your job: restore N-1 security by dispatching corrective actions.

DISPATCH PRIORITY (cheapest first):
1. BESS discharge  (10 €/MWh)
2. Curtail renewables (15-22 €/MWh)
3. Curtail load  (10,000-100,000 €/MWh — last resort)

CONSTRAINTS:
- Only dispatch actions from the provided action space.
- Do not exceed max_available_mw for any action.
- A negative sensitivity means the action RELIEVES the overload (good).
- Aim to bring max line loading below 100%.

OUTPUT FORMAT — respond with valid JSON only, no other text:
{
  "reasoning": "<2-4 sentence explanation of your decision>",
  "commands": [
    {"action_id": "<target_id>", "amount_mw": <float>},
    ...
  ]
}"""


def build_prompt(
    state: PostFaultState,
    action_space: list[ActionOption],
    iteration: int,
    history: list[CorrectiveIteration],
    weather: str = "clear",
    hour: int = 12,
    fault_name: str = "unknown",
) -> str:
    hist_txt = ""
    if history:
        hist_txt = "\n\nPREVIOUS ITERATIONS:\n"
        for it in history:
            hist_txt += (
                f"  Iter {it.iteration}: loading {it.max_loading_before:.1f}%→{it.max_loading_after:.1f}%"
                f"  cost={it.cost_eur:.0f} €/h\n"
                f"  actions: {[c.action_id for c in it.commands]}\n"
            )

    return f"""ITERATION {iteration}
Fault line: {fault_name}
Weather: {weather}  Hour: {hour:02d}:00

CURRENT GRID STATE:
{_fmt_state(state)}

FEASIBLE CORRECTIVE ACTIONS:
{_fmt_actions(action_space)}
{hist_txt}
Your dispatch decision (JSON only):"""


# ── LLM client ────────────────────────────────────────────────────────────────

def _parse_json_response(text: str) -> dict:
    """Extract JSON from model output (handles markdown code fences)."""
    text = text.strip()
    # strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _call_llm(messages: list[dict], api_key: str, model: str, dry_run: bool) -> str:
    if dry_run:
        # deterministic fallback without API call
        return json.dumps({
            "reasoning": (
                "No LLM API key provided — using rule-based fallback. "
                "Selecting the action with the best sensitivity-to-cost ratio "
                "(BESS discharge preferred)."
            ),
            "commands": []   # filled by caller
        })

    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.2,
        max_tokens=512,
    )
    return resp.choices[0].message.content


def _rule_based_commands(
    action_space: list[ActionOption],
    state: PostFaultState,
) -> list[DispatchCommand]:
    """Greedy rule-based dispatch when LLM is unavailable."""
    pp_needed = max(0.0, state.max_loading_pct - 95.0)
    cmds: list[DispatchCommand] = []
    remaining_pp = pp_needed

    for action in action_space:
        if remaining_pp <= 0.5:
            break
        if action.sensitivity >= 0:
            continue
        # MW needed = pp_needed / |sensitivity|
        mw = min(
            action.max_available_mw,
            remaining_pp / abs(action.sensitivity) if action.sensitivity != 0 else action.max_available_mw,
        )
        mw = round(max(0.01, mw), 3)
        cmds.append(DispatchCommand(action_id=action.target_id, amount_mw=mw))
        remaining_pp -= abs(action.sensitivity) * mw

    return cmds


# ── corrective loop ────────────────────────────────────────────────────────────

def run_corrective_loop(
    net,               # pp.pandapowerNet
    fault_line_idx: int,
    fault_line_name: str,
    action_space: list[ActionOption],
    initial_state: PostFaultState,
    api_key: Optional[str] = None,
    model: str = "deepseek-chat",
    max_iterations: int = 4,
    weather: str = "clear",
    hour: int = 12,
) -> tuple[list[CorrectiveIteration], "pp.pandapowerNet", PostFaultState]:
    """
    Run the LLM corrective control loop until N-1 secure or max_iterations.

    Returns (iterations, final_net, final_state).
    """
    from environment import apply_action, build_action_space

    dry_run = not api_key
    current_net = net
    current_state = initial_state
    history: list[CorrectiveIteration] = []
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for iteration in range(1, max_iterations + 1):
        if current_state.max_loading_pct < 100.0:
            break
        if not action_space:
            break

        prompt = build_prompt(
            current_state, action_space, iteration, history,
            weather=weather, hour=hour, fault_name=fault_line_name,
        )
        messages.append({"role": "user", "content": prompt})

        raw = _call_llm(messages, api_key or "", model, dry_run)
        messages.append({"role": "assistant", "content": raw})

        try:
            parsed = _parse_json_response(raw)
            reasoning = parsed.get("reasoning", "")
            cmds_raw = parsed.get("commands", [])
            commands = [
                DispatchCommand(action_id=c["action_id"], amount_mw=float(c["amount_mw"]))
                for c in cmds_raw
                if "action_id" in c and "amount_mw" in c
            ]
        except (json.JSONDecodeError, KeyError):
            reasoning = "Parse error — using rule-based fallback."
            commands = []

        if dry_run or not commands:
            commands = _rule_based_commands(action_space, current_state)
            if not reasoning.strip() or "No LLM" in reasoning:
                reasoning = (
                    "Rule-based dispatch: prioritising BESS discharge for lowest cost, "
                    "then renewable curtailment if further relief needed. "
                    "Load shedding avoided where possible."
                )

        # apply commands
        loading_before = current_state.max_loading_pct
        overloads_before = len(current_state.overloaded_lines)
        undervolt_before = current_state.undervolt_buses
        total_cost = 0.0
        applied: list[DispatchCommand] = []

        for cmd in commands:
            action = next((a for a in action_space if a.target_id == cmd.action_id), None)
            if action is None:
                continue
            mw = min(cmd.amount_mw, action.max_available_mw)
            if mw <= 0:
                continue
            current_net, current_state = apply_action(
                current_net, fault_line_idx, action, mw
            )
            total_cost += mw * action.cost_per_mw
            applied.append(DispatchCommand(
                action_id=cmd.action_id,
                amount_mw=round(mw, 3),
                reasoning_snippet=action.label,
            ))
            # remove exhausted actions
            action.max_available_mw = round(action.max_available_mw - mw, 3)

        history.append(CorrectiveIteration(
            iteration=iteration,
            reasoning=reasoning,
            commands=applied,
            max_loading_before=round(loading_before, 2),
            max_loading_after=round(current_state.max_loading_pct, 2),
            overloads_before=overloads_before,
            overloads_after=len(current_state.overloaded_lines),
            undervolt_before=undervolt_before,
            undervolt_after=current_state.undervolt_buses,
            cost_eur=round(total_cost, 2),
        ))

        # refresh action space after each iteration
        if current_state.max_loading_pct >= 100.0:
            action_space = build_action_space(current_net, fault_line_idx, current_state)

    return history, current_net, current_state
