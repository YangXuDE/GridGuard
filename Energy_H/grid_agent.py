"""
grid_agent.py — LLM Brain Module

Interfaces with the DeepSeek API (via OpenAI-compatible SDK) to produce
cost-optimal redispatch and curtailment decisions for grid overload relief.

Author: Energy x AI Hackathon — E.ON Grid Operation Agents Track
"""

import json
import os
import re
from typing import Any, Dict, List

from openai import OpenAI

from grid_environment import ActionOption, GridStateReport


# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a Senior Power Grid Operations Agent. Your task is to resolve line overloads after an N-1 contingency by issuing redispatch, storage, renewable-curtailment and load-curtailment commands.

## CONSTRAINTS (hard requirements — never violate)

1. **Action Space**: You may ONLY use target_id values that appear in the Action Space provided below. Do not invent new target ids.
2. **Capacity Limits**: For each action, `amount_mw` MUST be <= the `max_available_mw` listed for that target_id. Never exceed available margins. BESS margins already account for the battery's state of charge (1 hour of sustained output).
3. **Economic Optimization**: Choose actions that minimize **total economic cost**. The cost of each action is `amount_mw * cost_per_mw`. Cost order is usually: BESS charge/discharge (10 EUR/MWh wear) < renewable curtailment (50 EUR/MWh spilled) < generator redispatch (charged at the LIVE BALANCING PRICE shown in the context — expensive in scarcity hours) << load curtailment (10,000+ EUR/MWh). **Avoid over-procurement**: do not order more MW than strictly required to reach the 94% target. Every unnecessary MW wastes money.
4. **Critical Load Protection**: Never curtail a load with `cost_per_mw >= 100000` unless there is literally no other feasible action to save the grid.
5. **Direction Matters**: `redispatch_up` and `discharge_battery` INJECT power; `redispatch_down`, `charge_battery` and `curtail_renewable` WITHDRAW injection. On distribution feeders the head of the feeder is usually relieved by INJECTING downstream (discharge BESS, keep renewables) or reducing demand; reverse-flow overloads at solar noon are relieved by `charge_battery` or `curtail_renewable`. Use the sensitivity hints to determine which direction provides relief on each overloaded line.
6. **Storage / renewable target_ids**: BESS actions use string IDs like `"storage_0"`; renewable curtailment uses `"sgen_3"`. Use these exact strings — do NOT convert them to integers.
7. **Plan against the FORECAST**: the operational context lists the next hours of weather, renewable output, demand and balancing prices. Wind farms CUT OUT above 25 m/s; solar dies at sunset. Do not lean a fix on renewable output the forecast is about to remove, and keep battery energy in reserve when demand or prices are still rising. Derated (poor-health) lines overload sooner — they are listed in the asset-health context.

## OPERATIONAL DOCTRINE

5. **Sensitivity format**: Each action includes a sensitivity hint like:
   - `-0.089 pp relief on L128 per MW` → NEGATIVE = good (reduces loading).
     +0.229 pp increase on L128 per MW → POSITIVE = bad (increases loading).
   Use the magnitude to compute required MW. E.g., if line L128 is at 104% and target is 94%, you need 10 pp of relief. With -0.089 pp/MW, you need 10 / 0.089 ≈ 112 MW.
6. **Over-correction**: Target bringing EACH overloaded line to exactly 94% (not 100%, not 80%). This 94% target leaves a lean safety margin while minimizing procurement cost. Use the sensitivity hints to compute the precise MW needed — e.g., if line L128 is at 104% and target is 94%, you need 10 pp of relief. With -0.089 pp/MW, issue 10 / 0.089 ≈ 112 MW. Do NOT pad with +30% "just in case." Make precise micro-adjustments based on the sensitivity math. Overshooting wastes money; undershooting requires costly follow-up iterations.
7. **Multi-line overloads**: If multiple lines are overloaded, pick the cheapest action that provides relief on the MOST overloaded line first. Prefer actions with NEGATIVE sensitivity (relief). Avoid actions with POSITIVE sensitivity (increase) as they worsen the problem.

## OUTPUT FORMAT

Respond with a single JSON object containing exactly two keys:

{
  "reasoning": "<Brief explanation of cost vs. risk trade-offs. Mention why specific actions were chosen and how much MW was needed based on sensitivity.>",
  "actions": [
    {"target_id": <int|str>, "action_type": "<redispatch_up|redispatch_down|curtail_load|discharge_battery|charge_battery|curtail_renewable>", "amount_mw": <float>}
  ]
}

If no actions are needed, return an empty `actions` list.
Do NOT wrap your response in markdown code fences. Output raw JSON only."""


# ---------------------------------------------------------------------------
# GridAgent
# ---------------------------------------------------------------------------

class GridAgent:
    """
    LLM-based decision agent that reads grid state + action space and
    returns an economically optimized set of corrective actions.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        temperature: float = 0.1,
        max_tokens: int = 2048,
    ) -> None:
        """
        Initialize the OpenAI-compatible client pointed at DeepSeek.

        Args:
            api_key: DeepSeek API key.
            base_url: API endpoint (default: DeepSeek).
            model: Model name to use.
            temperature: Sampling temperature (keep low for deterministic
                         grid operations).
            max_tokens: Maximum tokens in the response.
        """
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_decision(
        self,
        grid_state: GridStateReport,
        action_space: List[ActionOption],
        context: str = "",
    ) -> Dict[str, Any]:
        """
        Build a prompt from grid state + action space, call the LLM,
        and return a parsed, validated decision dict.

        Args:
            grid_state: Structured grid state report.
            action_space: List of available actions.
            context: Operational context block (weather, forecast, market
                     prices, BESS state of charge, asset health) from
                     GridEnvironment.context_block().

        Returns:
            Dict with keys 'reasoning' (str) and 'actions' (list of dicts).
        """
        user_prompt = self._build_user_prompt(grid_state, action_space, context)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        raw_text = response.choices[0].message.content or ""
        return self._parse_response(raw_text, action_space)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user_prompt(
        grid_state: GridStateReport,
        action_space: List[ActionOption],
        context: str = "",
    ) -> str:
        """Assemble the user-facing prompt with grid state and action data."""

        # --- Grid State section ---
        if not grid_state.converged:
            state_lines = [
                "## Current Grid State",
                "Power flow DID NOT converge. The grid is unstable.",
                "You MUST issue corrective actions to restore stability.",
            ]
        elif grid_state.overloaded_lines:
            state_lines = [
                "## Current Grid State",
                f"Max line loading: {grid_state.max_loading_percent:.2f}%",
                "",
                "### Overloaded Lines:",
            ]
            for ol in grid_state.overloaded_lines:
                state_lines.append(
                    f"  - Line {ol.line_index} ('{ol.name}'): "
                    f"Bus {ol.from_bus} -> Bus {ol.to_bus}, "
                    f"{ol.loading_percent:.2f}% loading "
                    f"(in_service={ol.in_service})"
                )
            state_lines.append("")
            state_lines.append(grid_state.summary)
        else:
            state_lines = [
                "## Current Grid State",
                "No overloads detected. Grid is secure.",
            ]
        if grid_state.voltage_violations:
            state_lines.append("### Bus Voltage Violations:")
            for vv in grid_state.voltage_violations:
                state_lines.append(
                    f"  - Bus {vv['bus']} ('{vv['name']}'): {vv['vm_pu']} pu"
                )
        if context:
            state_lines = [context, ""] + state_lines

        # --- Action Space section ---
        as_lines = [
            "## Available Action Space",
            "You may select ONLY from the following actions:",
            "",
        ]
        as_lines.append(
            f"{'ID':<4} {'Type':<16} {'Bus':<6} "
            f"{'Curr MW':<10} {'Max Avail MW':<14} {'Cost/MWh':<12} "
            f"{'Critical':<10} {'Description'}"
        )
        as_lines.append("-" * 100)

        for a in action_space:
            critical_flag = (
                "YES"
                if a.cost_per_mw >= GridAgent._get_critical_threshold()
                else "no"
            )
            as_lines.append(
                f"{a.target_id:<4} {a.action_type:<16} {a.node_bus:<6} "
                f"{a.current_p_mw:<10.2f} {a.max_available_mw:<14.2f} "
                f"{a.cost_per_mw:<12.2f} {critical_flag:<10} "
                f"{a.description}"
            )

        # --- Economic guidance ---
        guidance = [
            "",
            "## Instructions",
            "1. Choose the CHEAPEST combination of actions that resolves ALL overloads.",
            "2. Target exactly 94% loading on each overloaded line — not 100%, not 80%.",
            "3. Use sensitivity hints to compute the PRECISE MW needed. Avoid over-procurement.",
            "4. Cost order: BESS (10) < curtail_renewable (50) < redispatch (balancing price) << curtail_load (10,000+ EUR/MWh).",
            "5. NEVER curtail a CRITICAL load (100,000 EUR/MWh) unless there is NO other option.",
            "6. Respect the forecast: no fixes that depend on renewables about to vanish (sunset, storm cut-out).",
            "7. Return ONLY valid JSON. No markdown, no extra text.",
        ]

        return "\n".join(state_lines + [""] + as_lines + guidance)

    @staticmethod
    def _get_critical_threshold() -> float:
        """Return the cost threshold above which a load is considered critical."""
        return 50_000.0

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @classmethod
    def _parse_response(
        cls,
        raw_text: str,
        action_space: List[ActionOption],
    ) -> Dict[str, Any]:
        """
        Extract and validate JSON from the LLM response.

        Steps:
          1. Strip markdown code fences (```json ... ```).
          2. Attempt JSON parse.
          3. Validate structure (must have 'reasoning' and 'actions').
          4. Validate each action against the provided action space.

        Returns a clean dict or a fallback with empty actions.
        """
        cleaned = cls._strip_markdown_fences(raw_text)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            print(f"[GridAgent] JSON parse error: {exc}")
            print(f"[GridAgent] Raw response was: {raw_text[:500]}...")
            return {"reasoning": "JSON parse failed.", "actions": []}

        if not isinstance(parsed, dict):
            print("[GridAgent] Response is not a JSON object.")
            return {"reasoning": "Response was not a dict.", "actions": []}

        reasoning = parsed.get("reasoning", "")
        actions = parsed.get("actions", [])

        if not isinstance(actions, list):
            print("[GridAgent] 'actions' is not a list. Discarding.")
            actions = []

        valid_actions = cls._validate_actions(actions, action_space)
        return {"reasoning": reasoning, "actions": valid_actions}

    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        """
        Remove markdown code fences and any leading/trailing whitespace.

        Handles:
          - ```json ... ```
          - ``` ... ```
          - Leading/trailing whitespace and newlines.
        """
        # Remove opening fence: ```json or ```
        text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.MULTILINE)
        # Remove closing fence: ```
        text = re.sub(r"\n?```\s*$", "", text)
        return text.strip()

    @staticmethod
    def _validate_actions(
        actions: List[Dict[str, Any]],
        action_space: List[ActionOption],
    ) -> List[Dict[str, Any]]:
        """
        Validate each LLM-issued action against the action space.

        Checks:
          - target_id exists in action space.
          - action_type matches the declared type for that target.
          - amount_mw <= max_available_mw (clamped if exceeded).
        """
        # Build lookup: target_id -> ActionOption
        space_map: Dict[tuple, ActionOption] = {}
        for opt in action_space:
            key = (opt.target_id, opt.action_type)
            space_map[key] = opt

        validated: List[Dict[str, Any]] = []

        for i, action in enumerate(actions):
            tid = action.get("target_id")
            atype = action.get("action_type")
            amt = action.get("amount_mw")

            if tid is None or atype is None or amt is None:
                print(
                    f"[GridAgent] Action {i} missing required fields: "
                    f"{action}. Skipping."
                )
                continue

            # Storage actions use string target_ids ("storage_N")
            if isinstance(tid, str) and tid.startswith("storage_"):
                pass  # keep as string
            else:
                try:
                    tid = int(tid)
                except (ValueError, TypeError):
                    print(
                        f"[GridAgent] Action {i} has non-numeric target_id: "
                        f"{tid}. Skipping."
                    )
                    continue

            try:
                amt = float(amt)
            except (ValueError, TypeError):
                print(
                    f"[GridAgent] Action {i} has non-numeric amount_mw: "
                    f"{amt}. Skipping."
                )
                continue

            key = (tid, atype)
            if key not in space_map:
                print(
                    f"[GridAgent] Action {i}: target_id={tid}, "
                    f"type='{atype}' not found in action space. Skipping."
                )
                continue

            option = space_map[key]
            if amt > option.max_available_mw + 0.01:
                print(
                    f"[GridAgent] Action {i}: amount_mw={amt:.2f} exceeds "
                    f"max_available_mw={option.max_available_mw:.2f}. "
                    f"Clamping."
                )
                amt = option.max_available_mw

            if amt <= 0:
                print(
                    f"[GridAgent] Action {i}: amount_mw={amt:.2f} is "
                    f"non-positive. Skipping."
                )
                continue

            validated.append(
                {
                    "target_id": tid,
                    "action_type": atype,
                    "amount_mw": round(amt, 2),
                }
            )

        return validated


# ---------------------------------------------------------------------------
# Convenience loader
# ---------------------------------------------------------------------------

def load_agent_from_env() -> GridAgent:
    """
    Factory that reads DEEPSEEK_API_KEY from the environment and returns
    a configured GridAgent.

    Raises:
        ValueError: If DEEPSEEK_API_KEY is not set.
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError(
            "Environment variable DEEPSEEK_API_KEY is not set. "
            "Export it or pass the key explicitly to GridAgent(...)."
        )
    return GridAgent(api_key=api_key)
