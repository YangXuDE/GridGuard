"""
main.py — Autonomous Grid Self-Healing Control Loop

Orchestrates the GridEnvironment (physics engine) and GridAgent (LLM brain)
to detect and resolve transmission line overloads after an N-1 contingency.

Execution flow (Two-Stage Pipeline):
  Stage 1 — Fast Physics Filter:
    1. Load grid, run initial power flow.
    2. Screen ALL N-1 contingencies via screen_all_contingencies().
    3. Rank lines by risk score; select the most dangerous one.
  Stage 2 — LLM Cognitive Layer:
    4. Trigger the selected N-1 fault.
    5. Enter a corrective loop (max 3 iterations):
       a. Check for overloads. If none -> SUCCESS, exit.
       b. Build action space.
       c. Query the LLM for a decision.
       d. Print the LLM's reasoning.
       e. Execute the LLM's actions on the grid.
    6. Print final grid status and economic summary.

Usage:
    python main.py [--network case14] [--line N] [--api-key KEY]

Author: Energy x AI Hackathon — E.ON Grid Operation Agents Track
"""

import argparse
import copy
import os
import sys
import time
from typing import Optional

from grid_environment import GridEnvironment
from grid_agent import GridAgent, load_agent_from_env


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_ITERATIONS = 3
DEFAULT_NETWORK = "ieee123"
SEPARATOR = "=" * 72
HITL_SEPARATOR = "-" * 56


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_screening_report(results: list, top_n: int = 5) -> None:
    """
    Print a formatted risk-screening summary.

    Highlights catastrophic (non-convergent) contingencies in red-hot
    language and shows the top-N dangerous-but-solvable contingencies.
    """
    total = len(results)
    n_critical = sum(1 for r in results if not r["converged"])
    n_island = sum(1 for r in results if r.get("islands"))
    n_dangerous = sum(1 for r in results if r["converged"] and r["n_overloaded"] > 0)
    n_safe = total - n_critical - n_dangerous - n_island
    n_ac = sum(1 for r in results if r.get("stage") == "ac")

    # Summary banner
    print()
    print("  " + "-" * 56)
    print(f"  SCREENING COMPLETE: {total} lines evaluated "
          f"(two-stage: DC all, AC x{n_ac})")
    print(f"    Catastrophic (non-convergent) : {n_critical}")
    print(f"    Islanding   (strands load)    : {n_island}")
    print(f"    Dangerous   (>100% overloads) : {n_dangerous}")
    print(f"    Safe        (N-1 secure)      : {n_safe}")
    print("  " + "-" * 56)
    islanding = [r for r in results if r.get("islands")]
    if islanding:
        worst = max(islanding, key=lambda r: r["unserved_kw"])
        print(f"    Worst islanding outage: {worst['name']} — "
              f"{worst['unserved_kw']:.0f} kW stranded "
              f"({worst['islands']} buses). Islanding outages need "
              f"restoration switching, not redispatch.")

    # Top-N dangerous contenders
    dangerous = [r for r in results if r["converged"] and r["n_overloaded"] > 0]
    if dangerous:
        print()
        print(f"  Top {min(top_n, len(dangerous))} Critical Vulnerabilities:")
        print(f"  {'Rank':<6} {'Line':<8} {'Name':<12} {'Overloads':<10} {'Max Load':<10} {'Risk Score':<12}")
        print(f"  {'-'*4}  {'-'*6}  {'-'*10}  {'-'*8}  {'-'*8}  {'-'*10}")
        for rank, r in enumerate(dangerous[:top_n], 1):
            name = r["name"] or "-"
            print(
                f"  #{rank:<5} {r['line_index']:<8} {name:<12} "
                f"{r['n_overloaded']:<10} {r['max_loading_pct']:<10.2f}% "
                f"{r['risk_score']:<12.2f}"
            )

    # Show catastrophic (non-convergent) entries
    catastrophic = [r for r in results if not r["converged"]]
    if catastrophic and len(catastrophic) <= 5:
        print()
        print(f"  Catastrophic Failures (voltage collapse / islanding): "
              f"{[r['line_index'] for r in catastrophic]}")


def _compute_economic_cost(
    actions: list, action_space: list
) -> float:
    """
    Compute the total economic cost (EUR/h) of a set of applied actions.

    Matches each action against its ActionOption to retrieve cost_per_mw,
    then sums amount_mw * cost_per_mw.
    """
    # Build lookup
    cost_map = {}
    for opt in action_space:
        cost_map[(opt.target_id, opt.action_type)] = opt.cost_per_mw

    total = 0.0
    for act in actions:
        key = (act.get("target_id"), act.get("action_type"))
        per_mw = cost_map.get(key, 0.0)
        total += float(act.get("amount_mw", 0.0)) * per_mw

    return total


def _print_header(text: str) -> None:
    """Print a formatted section header."""
    print()
    print(SEPARATOR)
    print(f"  {text}")
    print(SEPARATOR)


def _print_grid_state(state) -> None:
    """Print a human-readable grid state summary."""
    print(f"  Converged : {state.converged}")
    print(f"  Max loading : {state.max_loading_percent:.2f}%")
    print(f"  Overloaded lines : {len(state.overloaded_lines)}")
    if state.overloaded_lines:
        for ol in state.overloaded_lines:
            print(
                f"    - Line {ol.line_index} ('{ol.name}'): "
                f"Bus {ol.from_bus} -> Bus {ol.to_bus}  "
                f"[{ol.loading_percent:.2f}%]"
            )
    if getattr(state, "voltage_violations", None):
        print(f"  Voltage violations : {len(state.voltage_violations)}")
        for vv in state.voltage_violations[:5]:
            print(f"    - Bus {vv['bus']} ('{vv['name']}'): {vv['vm_pu']} pu")
    wn = getattr(state, "weather_now", None)
    if wn:
        print(
            f"  Weather : {wn['condition']}, {wn['temp_c']}°C, "
            f"wind {wn['wind_ms']} m/s — balancing "
            f"{wn['balancing_price']} EUR/MWh"
        )
    print(f"  Summary: {state.summary}")


def _print_benchmark_report(
    llm_result: dict,
    baseline_result: dict,
    fault_line: int,
) -> None:
    """Print a side-by-side Benchmark Report: LLM Agent vs. LP Baseline."""
    def _ok(status) -> str:
        return "SECURE" if status else "FAIL"

    print()
    print("=" * 72)
    print("  BENCHMARK REPORT — LLM Agent  vs.  LP Baseline (SCOPF-lite)")
    print("=" * 72)
    print(f"  Faulted line: {fault_line}")
    print()
    print(f"  {'Metric':<30} {'LLM Agent':<20} {'LP Baseline':<20}")
    print(f"  {'-'*30} {'-'*20} {'-'*20}")

    print(
        f"  {'Base-case secure':<30} "
        f"{_ok(llm_result['base_secure']):<20} "
        f"{_ok(baseline_result['base_secure']):<20}"
    )
    print(
        f"  {'N-1 secure':<30} "
        f"{_ok(llm_result['n1_secure']):<20} "
        f"{_ok(baseline_result['n1_secure']):<20}"
    )
    print(
        f"  {'Max loading %':<30} "
        f"{llm_result['max_loading_pct']:<20.2f}% "
        f"{baseline_result['max_loading_pct']:<20.2f}%"
    )
    print(
        f"  {'Total economic cost (EUR/h)':<30} "
        f"EUR {llm_result['cost_usd']:<19,.0f} "
        f"EUR {baseline_result['cost_usd']:<19,.0f}"
    )
    print(
        f"  {'Compute time (s)':<30} "
        f"{llm_result['time_s']:<20.2f} "
        f"{baseline_result['time_s']:<20.2f}"
    )
    print(
        f"  {'Actions taken':<30} "
        f"{llm_result['n_actions']:<20} "
        f"{baseline_result['n_actions']:<20}"
    )

    # Cost efficiency
    llm_cost = llm_result["cost_usd"]
    bl_cost = baseline_result["cost_usd"]
    if bl_cost > 0:
        ratio = llm_cost / bl_cost
        winner = "LLM Agent" if llm_cost < bl_cost else "LP Baseline"
        print()
        print(f"  Cost ratio (LLM / Baseline): {ratio:.2f}x  —  {winner} cheaper")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Main control loop
# ---------------------------------------------------------------------------

def main(
    network_name: str = DEFAULT_NETWORK,
    line_index: Optional[int] = None,
    line_rating_factor: float = 1.0,
    fixed_max_i_ka: Optional[float] = None,
    api_key: Optional[str] = None,
    non_interactive: bool = False,
    benchmark: bool = False,
    load_scale: float = 1.0,
    weather: Optional[str] = "clear",
    weather_hour: int = 14,
    with_der: bool = True,
) -> int:
    """
    Run the autonomous grid self-healing loop with Human-in-the-Loop gates.

    Args:
        network_name: pandapower network name (e.g. 'case118', 'case300').
        line_index: Specific line to trip (None = auto-select most loaded).
        line_rating_factor: Multiplier applied to all line max_i_ka.
        fixed_max_i_ka: Override all line max_i_ka to this value (kA).
        api_key: DeepSeek API key (falls back to DEEPSEEK_API_KEY env var).
        non_interactive: If True, skip HITL prompts (auto-approve). Useful
                         for automated testing or CI pipelines.
        benchmark: If True, run both LLM agent and LP baseline side-by-side
                   and print a comparison report. Forces non_interactive.

    Returns:
        0 on success (overloads resolved), 1 on failure.
    """
    # ---- Resolve API key --------------------------------------------------
    api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("ERROR: DEEPSEEK_API_KEY is not set.", file=sys.stderr)
        print(
            "Export it or pass --api-key.", file=sys.stderr
        )
        return 1

    print(SEPARATOR)
    print("  Grid Self-Healing Agent — E.ON Hackathon (DeepSeek + pandapower)")
    print(f"  Network: {network_name}")
    print(SEPARATOR)

    # ---- Step 1: Initialize environment -----------------------------------
    _print_header("Step 1: Initializing Grid Environment")
    env = GridEnvironment(
        network_name=network_name,
        line_rating_factor=line_rating_factor,
        fixed_max_i_ka=fixed_max_i_ka,
        load_scale=load_scale,
        weather=weather,
        weather_hour=weather_hour,
        with_der=with_der,
    )
    context = env.context_block()
    if context:
        print()
        for line in context.splitlines():
            print(f"  {line}")

    # ---- Step 2: Initial power flow ---------------------------------------
    _print_header("Step 2: Running Initial Power Flow (Pre-Fault)")
    converged = env.run_powerflow()
    if not converged:
        print("  Base-case power flow did not converge. Aborting.")
        return 1
    initial_state = env.get_grid_state()
    _print_grid_state(initial_state)

    # ========================================================================
    #  STAGE 1 — Fast Physics Filter: Screen ALL N-1 contingencies
    # ========================================================================

    if line_index is not None:
        # Operator explicitly specified a line — skip screening
        _print_header("Stage 1: N-1 Contingency (User-Specified)")
        fault_line = line_index
        print(f"\n  Operator-specified line: {fault_line}")
    else:
        _print_header("Stage 1: Fast Risk Screening — Evaluating ALL N-1 Contingencies")
        print(f"\n  Scanning {len(env.net.line)} lines for N-1 vulnerabilities...")
        all_risks = env.screen_all_contingencies()
        _print_screening_report(all_risks)

        # Select the #1 most dangerous line
        dangerous = [r for r in all_risks if r["converged"] and r["n_overloaded"] > 0]
        if not dangerous:
            print("\n  Grid is fully N-1 secure. No intervention needed.")
            return 0

        fault_line = dangerous[0]["line_index"]
        print(f"\n  >>> Escalating #{1} threat to Stage 2: "
              f"Line {fault_line} "
              f"(risk score {dangerous[0]['risk_score']:.2f}, "
              f"{dangerous[0]['n_overloaded']} overload(s) at "
              f"{dangerous[0]['max_loading_pct']:.2f}%)")

    # ---- Trigger the selected N-1 fault -----------------------------------
    _print_header("Stage 2: Triggering Selected N-1 Contingency")
    env.trigger_n_1_fault(fault_line)
    env.run_powerflow()
    post_fault_state = env.get_grid_state()
    _print_grid_state(post_fault_state)

    if not post_fault_state.overloaded_lines:
        print("\n  No overloads after N-1 fault. Grid is N-1 secure.")
        return 0

    # ========================================================================
    #  BENCHMARK MODE — Side-by-side LLM vs LP Baseline comparison
    # ========================================================================
    if benchmark:
        non_interactive = True  # force auto-run

        # ---- Deep-copy the post-fault environment for the baseline track ----
        env_bl = copy.deepcopy(env)

        # =====================================================================
        #  Track 1 — LLM Agent
        # =====================================================================
        _print_header("BENCHMARK — Track 1: LLM Agent Resolution")
        agent = GridAgent(api_key=api_key)
        print(f"  Model       : {agent.model}")
        print(f"  Temperature : {agent.temperature}")

        t0_llm = time.perf_counter()
        llm_total_cost = 0.0
        llm_n_actions = 0
        llm_iterations = 0

        for iteration in range(1, MAX_ITERATIONS + 1):
            llm_iterations = iteration
            state = env.get_grid_state()
            if not state.overloaded_lines:
                print("  [LLM] No overloads remaining. Grid is secure.")
                break

            action_space = env.get_action_space(grid_state=state)
            if not action_space:
                print("  [LLM] WARNING: Action space is empty.")
                break

            try:
                decision = agent.get_decision(state, action_space,
                                              context=env.context_block())
            except Exception as exc:
                print(f"  [LLM] API call failed: {exc}")
                break
            actions = decision.get("actions", [])
            if not actions:
                print("  [LLM] No actions proposed. Ending loop.")
                break

            print(f"  [LLM] Iter {iteration}: {len(actions)} action(s) proposed")
            for act in actions:
                print(f"         [{act['action_type']}] target={act['target_id']} "
                      f"amount={act['amount_mw']:.2f} MW")

            iter_cost = _compute_economic_cost(actions, action_space)
            llm_total_cost += iter_cost
            llm_n_actions += len(actions)

            if not env.execute_actions(actions):
                print("  [LLM] Action execution failed. Aborting.")
                break

        t1_llm = time.perf_counter()
        llm_time = t1_llm - t0_llm

        llm_final = env.get_grid_state()
        llm_base_secure = not llm_final.overloaded_lines and llm_final.converged
        llm_n1_secure = llm_base_secure  # LLM doesn't re-screen N-1

        print(f"\n  [LLM] Complete: {llm_time:.2f}s, "
              f"cost={llm_total_cost:,.0f} EUR/h, "
              f"base_secure={llm_base_secure}")

        # =====================================================================
        #  Track 2 — LP Baseline (SCOPF-lite)
        # =====================================================================
        _print_header("BENCHMARK — Track 2: LP Baseline Resolution")
        from env import GridEnv
        from baseline import run_baseline

        grid_env_bl = GridEnv(env_bl)
        print(f"  Solver        : scipy.linprog (HiGHS)")
        print(f"  Max rounds    : 6")
        print(f"  Max ctg nets  : 8")

        t0_bl = time.perf_counter()
        bl_result = run_baseline(grid_env_bl, target_n1=True, verbose=True)
        t1_bl = time.perf_counter()
        bl_time = t1_bl - t0_bl

        # Get max loading from the baseline-corrected grid
        bl_final_state = env_bl.get_grid_state()
        bl_max_loading = bl_final_state.max_loading_percent

        print(f"\n  [Baseline] Complete: {bl_time:.2f}s, "
              f"cost={bl_result['cost_usd']:,.0f} EUR/h, "
              f"base_secure={bl_result['base_secure']}")

        # =====================================================================
        #  Benchmark Report
        # =====================================================================
        _print_benchmark_report(
            llm_result={
                "base_secure": llm_base_secure,
                "n1_secure": llm_n1_secure,
                "max_loading_pct": llm_final.max_loading_percent,
                "cost_usd": llm_total_cost,
                "time_s": llm_time,
                "n_actions": llm_n_actions,
            },
            baseline_result={
                "base_secure": bl_result["base_secure"],
                "n1_secure": bl_result["n1_secure"] or False,
                "max_loading_pct": bl_max_loading,
                "cost_usd": bl_result["cost_usd"],
                "time_s": bl_time,
                "n_actions": bl_result["n_actions"],
            },
            fault_line=fault_line,
        )

        return 0

    # ========================================================================
    #  STAGE 2 — LLM Cognitive Layer: Resolve overloads
    # ========================================================================
    _print_header("Stage 2: Initializing GridAgent (DeepSeek LLM)")
    agent = GridAgent(api_key=api_key)
    print(f"  Model       : {agent.model}")
    print(f"  Temperature : {agent.temperature}")
    print(f"  Max tokens  : {agent.max_tokens}")

    # ---- Stage 2 (cont.): AI Corrective Control Loop -----------------------
    total_cost = 0.0

    for iteration in range(1, MAX_ITERATIONS + 1):
        _print_header(f"Stage 2.{iteration}: AI Corrective Iteration {iteration}/{MAX_ITERATIONS}")

        # (a) Check if still overloaded
        state = env.get_grid_state()
        if not state.overloaded_lines:
            print("  No overloads remaining. Grid is secure.")
            break

        print(f"  Active overloads: {len(state.overloaded_lines)}")
        for ol in state.overloaded_lines:
            print(
                f"    Line {ol.line_index} ('{ol.name}'): "
                f"{ol.loading_percent:.2f}%"
            )

        # (b) Build action space
        action_space = env.get_action_space(grid_state=state)
        if not action_space:
            print("  WARNING: Action space is empty. Cannot resolve overloads.")
            break

        print(f"  Available actions: {len(action_space)}")
        for a in action_space:
            print(f"    [{a.target_id}] {a.action_type:<16} "
                  f"bus={a.node_bus}  max_avail={a.max_available_mw:.2f} MW  "
                  f"cost={a.cost_per_mw:.2f} EUR/MWh")

        # (c) Query LLM (with weather / market / asset-health context)
        print(f"\n  Querying DeepSeek LLM for decision...")
        try:
            decision = agent.get_decision(state, action_space,
                                          context=env.context_block())
        except Exception as exc:
            print(f"  [ERROR] LLM API call failed: {exc}")
            break

        # (d) Print AI reasoning
        reasoning = decision.get("reasoning", "")
        print()
        print(HITL_SEPARATOR)
        print("  [AI THINKING]  LLM Reasoning & Proposed Actions")
        print(HITL_SEPARATOR)
        for line in reasoning.splitlines():
            print(f"  {line}")

        # (e) Present actions for human approval
        actions = decision.get("actions", [])
        if not actions:
            print("\n  [AI] No actions proposed. Ending corrective loop.")
            break

        print()
        print(f"  Proposed Actions ({len(actions)}):")
        for act in actions:
            print(
                f"    [{act['action_type']}]  "
                f"target_id={act['target_id']}  "
                f"amount_mw={act['amount_mw']:.2f}"
            )

        iter_cost = _compute_economic_cost(actions, action_space)
        print(f"\n  Estimated Cost : {iter_cost:,.2f} EUR/h")

        # (f) Human-in-the-Loop gate
        if non_interactive:
            print(f"\n  [HITL] Non-interactive mode -- auto-approving actions.")
            approved = True
        else:
            print()
            print(HITL_SEPARATOR)
            print("  [HUMAN-IN-THE-LOOP]  AI Proposal Ready")
            print(HITL_SEPARATOR)
            user_input = input(
                "\n  [HITL] Approve and execute these actions? [y/N]: "
            ).strip().lower()
            approved = user_input in ("y", "yes")

        if not approved:
            print("\n  [HITL] EXECUTION ABORTED by human operator.")
            print("  [HITL] Corrective loop terminated.")
            break

        print("\n  [HITL] APPROVED. Executing actions on the physical grid...")

        total_cost += iter_cost
        success = env.execute_actions(actions)
        if not success:
            print("  [ERROR] Action execution failed. Aborting loop.")
            break

    # ---- Final report ------------------------------------------------------
    _print_header("Final Report: Post-Correction Grid Status")
    final_state = env.get_grid_state()
    _print_grid_state(final_state)

    print()
    print(f"  Total Economic Cost : {total_cost:,.2f} EUR/h")
    print(f"  Iterations Used     : {iteration}/{MAX_ITERATIONS}")

    if not final_state.overloaded_lines and final_state.converged:
        print("\n  RESULT: SUCCESS — Grid secured after N-1 contingency.")
        return 0
    else:
        print("\n  RESULT: FAILURE — Overloads remain after maximum iterations.")
        return 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Grid Self-Healing Agent — E.ON Hackathon"
    )
    parser.add_argument(
        "--network",
        default=DEFAULT_NETWORK,
        help=f"pandapower network name (default: {DEFAULT_NETWORK})",
    )
    parser.add_argument(
        "--line-rating",
        type=float,
        default=1.0,
        help="Line rating multiplier (default: 1.0)",
    )
    parser.add_argument(
        "--fixed-max-i-ka",
        type=float,
        default=None,
        help="Override all line max_i_ka to this value in kA",
    )
    parser.add_argument(
        "--line",
        type=int,
        default=None,
        help="Specific line index to trip (default: auto-select most loaded non-slack)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="DeepSeek API key (falls back to DEEPSEEK_API_KEY env var)",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=False,
        help="Skip HITL prompts (auto-approve all AI proposals)",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        default=False,
        help="Run LLM Agent vs LP Baseline side-by-side comparison",
    )
    parser.add_argument(
        "--load-scale",
        type=float,
        default=1.0,
        help="Multiplier on every load (stress knob, default 1.0)",
    )
    parser.add_argument(
        "--weather",
        choices=["clear", "heatwave", "overcast", "storm", "none"],
        default="clear",
        help="Weather profile driving solar, wind and demand (default clear)",
    )
    parser.add_argument(
        "--hour",
        type=int,
        default=14,
        help="Hour of day the scenario starts at, 0-23 (default 14)",
    )
    parser.add_argument(
        "--no-der",
        action="store_true",
        default=False,
        help="Disable rooftop PV / wind / utility PV / BESS placement",
    )
    args = parser.parse_args()

    exit_code = main(
        network_name=args.network,
        line_index=args.line,
        line_rating_factor=args.line_rating,
        fixed_max_i_ka=args.fixed_max_i_ka,
        api_key=args.api_key,
        non_interactive=args.non_interactive,
        benchmark=args.benchmark,
        load_scale=args.load_scale,
        weather=None if args.weather == "none" else args.weather,
        weather_hour=args.hour,
        with_der=not args.no_der,
    )
    sys.exit(exit_code)
