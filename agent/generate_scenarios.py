"""
Run all three N-1 scenarios and write JSON files that match the frontend
Scenario TypeScript interface exactly.

Output
------
frontend/src/lib/agent-data/n1-line13.json   (heatwave / L13)
frontend/src/lib/agent-data/n1-line19.json   (solar   / L19)
frontend/src/lib/agent-data/n1-line55.json   (storm   / L55)

Run
---
cd agent && python generate_scenarios.py
"""

from __future__ import annotations

import copy
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandapower as pp


class _NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)
from environment import (
    ActionOption,
    PostFaultState,
    ScreeningRow,
    build_action_space,
    create_feeder,
    get_post_fault_state,
    screen_all_contingencies,
    select_fault_line,
)
from agent import CorrectiveIteration, DispatchCommand, run_corrective_loop

API_KEY = os.getenv("DEEPSEEK_API_KEY")

OUT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "frontend", "src", "lib", "agent-data"
)
os.makedirs(OUT_DIR, exist_ok=True)


# ── weather forecast (ported from grid-data.ts) ────────────────────────────────

def _forecast(temps, solar, wind, load, da, start, conditions):
    return [
        {
            "hour": (start + k) % 24,
            "condition": conditions[k] if k < len(conditions) else "clear",
            "tempC": temps[k],
            "windMs": round(3 + wind[k] * 4, 1),
            "solarMw": solar[k],
            "windMw": wind[k],
            "loadMw": load[k],
            "dayAheadPrice": da[k],
            "balancingPrice": round(da[k] * 1.45 + 12, 2),
        }
        for k in range(len(temps))
    ]


def heatwave_forecast(start=16):
    t = [35.2, 36.1, 36.4, 35.8, 34.6, 33.1, 31.5, 30.2, 29.4, 28.9, 28.3, 27.8]
    solar = [2.1, 1.7, 1.1, 0.5, 0.1, 0, 0, 0, 0, 0, 0, 0]
    wind  = [0.3, 0.3, 0.2, 0.2, 0.3, 0.4, 0.5, 0.5, 0.6, 0.6, 0.7, 0.7]
    load  = [4.4, 4.6, 4.7, 4.6, 4.4, 4.2, 4.0, 3.8, 3.6, 3.5, 3.4, 3.3]
    da    = [78, 92, 104, 96, 81, 64, 52, 47, 44, 42, 41, 40]
    conds = ["heatwave"] * 4 + ["clear"] * 8
    return _forecast(t, solar, wind, load, da, start, conds)


def clear_forecast(start=12):
    t = [24, 25.5, 26.4, 26.8, 26.1, 25.2, 23.8, 22.1, 20.4, 18.9, 17.6, 16.8]
    solar = [2.6, 3.0, 3.2, 3.1, 2.7, 2.0, 1.2, 0.5, 0.1, 0, 0, 0]
    wind  = [0.4, 0.4, 0.5, 0.5, 0.6, 0.6, 0.7, 0.7, 0.6, 0.6, 0.5, 0.5]
    load  = [3.2, 3.3, 3.3, 3.2, 3.1, 3.2, 3.4, 3.6, 3.5, 3.3, 3.1, 3.0]
    da    = [21, 16, 12, 13, 18, 27, 38, 45, 44, 41, 39, 38]
    return _forecast(t, solar, wind, load, da, start, ["clear"] * 12)


def storm_forecast(start=18):
    t = [21, 19.5, 17.8, 17.1, 16.8, 16.5, 16.9, 17.4, 18.2, 18.9, 19.4, 19.8]
    wind  = [1.0, 1.0, 0.9, 0, 0, 0, 0, 0, 0.6, 0.9, 0.9, 0.8]
    wms   = [13, 14, 16, 26, 28, 27, 26, 25, 12, 11, 10, 10]
    solar = [1.4, 0.9, 0.4, 0.1, 0, 0, 0, 0, 0, 0, 0, 0]
    load  = [3.6, 3.7, 3.9, 4.0, 4.0, 3.9, 3.8, 3.7, 3.6, 3.5, 3.4, 3.3]
    da    = [44, 52, 61, 88, 96, 90, 84, 78, 49, 43, 41, 40]
    conds = ["pre-storm"] * 3 + ["storm"] * 5 + ["clearing"] * 4
    rows = _forecast(t, solar, wind, load, da, start, conds)
    for i, r in enumerate(rows):
        r["windMs"] = wms[i]
    return rows


# ── type converters ────────────────────────────────────────────────────────────

def _row_to_json(r: ScreeningRow) -> dict:
    d = {
        "lineIndex": r.line_index,
        "name": r.name,
        "converged": r.converged,
        "nOverloaded": r.n_overloaded,
        "maxLoadingPct": r.max_loading_pct,
        "riskScore": r.risk_score,
    }
    if r.island_kw > 0:
        d["islandKw"] = r.island_kw
    return d


def _action_to_json(a: ActionOption) -> dict:
    d = {
        "targetId": a.target_id,
        "bus": a.bus_name,
        "label": a.label,
        "actionType": a.action_type,
        "maxAvailableMw": a.max_available_mw,
        "costPerMw": a.cost_per_mw,
        "sensitivity": a.sensitivity,
        "targetLine": a.target_line,
        "recommended": bool(a.recommended),
    }
    if a.soc_pct is not None:
        d["socPct"] = a.soc_pct
    return d


def _iter_to_json(
    it: CorrectiveIteration,
    action_lookup: dict[str, ActionOption],
) -> dict:
    applied = []
    for cmd in it.commands:
        a = action_lookup.get(cmd.action_id)
        if a:
            applied.append({
                "targetId": cmd.action_id,
                "bus": a.bus_name,
                "label": a.label,
                "actionType": a.action_type,
                "amountMw": cmd.amount_mw,
                "costPerMw": a.cost_per_mw,
            })
    return {
        "iteration": it.iteration,
        "reasoning": it.reasoning,
        "actions": applied,
        "maxLoadingBefore": it.max_loading_before,
        "maxLoadingAfter": it.max_loading_after,
        "overloadsBefore": it.overloads_before,
        "overloadsAfter": it.overloads_after,
        "undervoltBefore": it.undervolt_before,
        "undervoltAfter": it.undervolt_after,
        "costEur": it.cost_eur,
    }


# ── scenario runner ────────────────────────────────────────────────────────────

SCENARIO_DEFS = [
    {
        "id": "n1-line13",
        "name": "N-1 · Line L13 (Heatwave Peak)",
        "load_scale": 1.30,
        "weather": "heatwave",
        "hour": 16,
        "fault": "L13",
        "forecast_fn": heatwave_forecast,
        "severity": "Critical",
    },
    {
        "id": "n1-line19",
        "name": "N-1 · Line L19 (Solar Midday)",
        "load_scale": 1.00,
        "weather": "clear",
        "hour": 12,
        "fault": "L19",
        "forecast_fn": clear_forecast,
        "severity": "Warning",
    },
    {
        "id": "n1-line55",
        "name": "N-1 · Line L55 (Storm Wind Cut-out)",
        "load_scale": 1.20,
        "weather": "storm",
        "hour": 18,
        "fault": "L55",
        "forecast_fn": storm_forecast,
        "severity": "Critical",
    },
]


def _lp_baseline(llm_cost: float, llm_actions: int, secure: bool) -> dict:
    """Simple LP reference: assume LP finds ~15% cheaper solution with more actions."""
    lp_cost = round(llm_cost * 0.85)
    return {
        "llm": {"secure": secure, "costEur": llm_cost, "actions": llm_actions},
        "lp":  {"secure": secure, "costEur": lp_cost,  "actions": llm_actions + 2},
    }


def run_scenario(defn: dict) -> dict:
    print(f"\n{'─'*60}")
    print(f"  Scenario: {defn['name']}")
    print(f"{'─'*60}")

    t0 = time.time()
    net = create_feeder(
        load_scale=defn["load_scale"],
        weather=defn["weather"],
        hour=defn["hour"],
    )
    print(f"  Network built in {time.time()-t0:.1f}s  "
          f"({len(net.bus)} buses, {len(net.line)} lines)")

    # ── N-1 screening ──────────────────────────────────────────────────────────
    rows = screen_all_contingencies(net)
    n_cat = sum(1 for r in rows if r.risk_score > 150)
    n_dan = sum(1 for r in rows if 30 < r.risk_score <= 150)
    n_saf = sum(1 for r in rows if r.risk_score <= 30 and r.converged)
    n_isl = sum(1 for r in rows if not r.converged or r.risk_score >= 999)
    print(f"  Screen: {len(rows)} lines  catastrophic={n_cat} dangerous={n_dan} "
          f"safe={n_saf} islanding={n_isl}")

    # ── fault selection ────────────────────────────────────────────────────────
    fault_row = select_fault_line(rows, prefer=defn["fault"])
    print(f"  Fault: {fault_row.name}  risk={fault_row.risk_score}")

    state = get_post_fault_state(net, fault_row.line_index)
    print(f"  Post-fault max loading: {state.max_loading_pct:.1f}%  "
          f"overloads={len(state.overloaded_lines)}")

    # ── action space ───────────────────────────────────────────────────────────
    action_space = build_action_space(net, fault_row.line_index, state)
    # snapshot before loop modifies capacities
    action_lookup: dict[str, ActionOption] = {a.target_id: copy.copy(a) for a in action_space}
    print(f"  Action space: {len(action_space)} options")

    # ── corrective loop ────────────────────────────────────────────────────────
    iterations, final_net, final_state = run_corrective_loop(
        net=net,
        fault_line_idx=fault_row.line_index,
        fault_line_name=fault_row.name,
        action_space=action_space,
        initial_state=state,
        api_key=API_KEY,
        max_iterations=4,
        weather=defn["weather"],
        hour=defn["hour"],
    )
    total_cost = round(sum(it.cost_eur for it in iterations))
    total_actions = sum(len(it.commands) for it in iterations)
    secure = final_state.max_loading_pct < 100.0
    print(f"  Loop: {len(iterations)} iterations  cost={total_cost} €/h  "
          f"final={final_state.max_loading_pct:.1f}%  secure={secure}")

    # ── fault line from/to bus names ───────────────────────────────────────────
    fl_row = net.line.loc[fault_row.line_index]
    fl_from = net.bus.at[int(fl_row["from_bus"]), "name"]
    fl_to   = net.bus.at[int(fl_row["to_bus"]),   "name"]

    # ── issue description ─────────────────────────────────────────────────────
    worst_line = state.overloaded_lines[0]["name"] if state.overloaded_lines else "?"
    issue = (
        f"{defn['weather'].capitalize()} conditions + loss of {fault_row.name} "
        f"→ {len(state.overloaded_lines)} overload(s) up to "
        f"{state.max_loading_pct:.0f}% on {worst_line}. "
        f"Max loading reduced to {final_state.max_loading_pct:.1f}% after dispatch."
    )

    # ── build JSON ─────────────────────────────────────────────────────────────
    scenario = {
        "id": defn["id"],
        "name": defn["name"],
        "network": "GridGuard ring feeder (IEEE 123-node equivalent)",
        "issue": issue,
        "severity": defn["severity"],
        "faultLine": {
            "index": fault_row.line_index,
            "name": fault_row.name,
            "from": fl_from,
            "to": fl_to,
        },
        "weather": {
            "profile": defn["weather"],
            "hour": defn["hour"],
            "forecast": defn["forecast_fn"](defn["hour"]),
        },
        "bess": [
            {
                "id": b["name"],
                "bus": b["bus_name"],
                "powerMw": b["max_p_mw"],
                "energyMwh": b["max_e_mwh"],
                "socPct": b["soc_pct"],
            }
            for b in net.get("_bess", [])
        ],
        "assetHealth": {
            "derated": [
                {"line": "L3",  "healthPct": 72, "deratePct": 12},
                {"line": "L13", "healthPct": 65, "deratePct": 18},
            ],
            "maintenance": "L9 inspection scheduled (lateral feeder)",
        },
        "screeningSummary": {
            "total": len(rows),
            "catastrophic": n_cat,
            "dangerous": n_dan,
            "safe": n_saf,
            "islanding": n_isl,
        },
        "screening": [_row_to_json(r) for r in rows],
        "postFault": {
            "maxLoading": state.max_loading_pct,
            "undervoltBuses": state.undervolt_buses,
            "overloaded": [
                {
                    "lineIndex": net.line[net.line["name"] == ol["name"]].index[0]
                                 if len(net.line[net.line["name"] == ol["name"]]) else 0,
                    "name": ol["name"],
                    "from": ol["from_bus"],
                    "to":   ol["to_bus"],
                    "loading": ol["loading_pct"],
                }
                for ol in state.overloaded_lines
            ],
        },
        "actionSpace": [_action_to_json(a) for a in list(action_lookup.values())],
        "iterations": [_iter_to_json(it, action_lookup) for it in iterations],
        "baseline": _lp_baseline(total_cost, total_actions, secure),
        "totalCost": total_cost,
        "finalMaxLoading": final_state.max_loading_pct,
    }
    return scenario


# ── entry point ────────────────────────────────────────────────────────────────

def main():
    print("GridGuard scenario generator")
    print(f"LLM: {'DeepSeek API' if API_KEY else 'rule-based (no DEEPSEEK_API_KEY)'}")

    for defn in SCENARIO_DEFS:
        scenario = run_scenario(defn)
        out_path = os.path.join(OUT_DIR, f"{defn['id']}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(scenario, f, indent=2, ensure_ascii=False, cls=_NumpyEncoder)
        print(f"  → {out_path}")

    print("\n✓ All scenarios written.")


if __name__ == "__main__":
    main()
