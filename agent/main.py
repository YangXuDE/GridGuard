#!/usr/bin/env python3
"""
GridGuard — CLI entry point.

Usage
─────
# dry-run (no LLM key, rule-based dispatch)
python main.py

# with DeepSeek API key
python main.py --api-key sk-...

# specific scenario
python main.py --scenario heatwave --fault L13 --api-key sk-...

# just run the N-1 screen (no corrective loop)
python main.py --screen-only
"""

import argparse
import os
import sys
import time

from environment import (
    create_feeder,
    screen_all_contingencies,
    select_fault_line,
    get_post_fault_state,
    build_action_space,
)
from agent import run_corrective_loop


# ── colour helpers ─────────────────────────────────────────────────────────────

def _c(code: str, text: str) -> str:
    """ANSI colour wrap (disabled when not a TTY)."""
    if not sys.stdout.isatty():
        return text
    codes = {"red": "31", "green": "32", "yellow": "33",
             "cyan": "36", "bold": "1", "dim": "2", "reset": "0"}
    return f"\033[{codes.get(code, '0')}m{text}\033[0m"


def _bar(pct: float, width: int = 20) -> str:
    filled = min(width, int(pct / 100 * width))
    bar = "█" * filled + "░" * (width - filled)
    if pct >= 100:
        return _c("red", bar)
    elif pct >= 80:
        return _c("yellow", bar)
    return _c("green", bar)


# ── scenario presets ───────────────────────────────────────────────────────────

SCENARIOS = {
    "heatwave": dict(load_scale=1.30, weather="heatwave", hour=16, fault="L13"),
    "solar":    dict(load_scale=1.00, weather="clear",    hour=12, fault="L19"),
    "storm":    dict(load_scale=1.20, weather="storm",    hour=18, fault="L55"),
}


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GridGuard N-1 self-healing agent")
    parser.add_argument("--scenario", choices=list(SCENARIOS), default="heatwave",
                        help="Preset scenario (default: heatwave)")
    parser.add_argument("--load-scale", type=float, default=None,
                        help="Override load scale (e.g. 1.3)")
    parser.add_argument("--weather", choices=["clear", "heatwave", "storm"],
                        default=None, help="Override weather profile")
    parser.add_argument("--hour", type=int, default=None,
                        help="Override dispatch hour (0-23)")
    parser.add_argument("--fault", default=None,
                        help="Force a specific fault line name (e.g. L13)")
    parser.add_argument("--api-key", default=os.getenv("DEEPSEEK_API_KEY"),
                        help="DeepSeek API key (or set DEEPSEEK_API_KEY env var)")
    parser.add_argument("--model", default="deepseek-chat",
                        help="DeepSeek model name (default: deepseek-chat)")
    parser.add_argument("--screen-only", action="store_true",
                        help="Only run N-1 screening, skip corrective loop")
    parser.add_argument("--max-iterations", type=int, default=4)
    args = parser.parse_args()

    preset = SCENARIOS[args.scenario]
    load_scale = args.load_scale or preset["load_scale"]
    weather    = args.weather    or preset["weather"]
    hour       = args.hour       or preset["hour"]
    fault_pref = args.fault      or preset["fault"]

    print(_c("bold", "\n╔══════════════════════════════════════════════════╗"))
    print(_c("bold",   "║          E.ON GridGuard — Self-Healing Agent     ║"))
    print(_c("bold",   "╚══════════════════════════════════════════════════╝"))
    print(f"  Scenario  : {args.scenario}  (load×{load_scale}, {weather}, {hour:02d}:00)")
    print(f"  Fault hint: {fault_pref}")
    print(f"  LLM mode  : {'DeepSeek API (' + args.model + ')' if args.api_key else 'rule-based (no API key)'}")

    # ── 1. build network ──────────────────────────────────────────────────────
    print(_c("cyan", "\n▶ Building distribution feeder…"))
    t0 = time.time()
    net = create_feeder(load_scale=load_scale, weather=weather, hour=hour)
    print(f"  Buses: {len(net.bus)}  Lines: {len(net.line)}  "
          f"Loads: {net.load['p_mw'].sum():.2f} MW  "
          f"Generation: {net.sgen['p_mw'].sum():.2f} MW")

    # ── 2. N-1 screening ──────────────────────────────────────────────────────
    print(_c("cyan", "\n▶ Stage 1 + 2 — N-1 Contingency Screening…"))
    rows = screen_all_contingencies(net)
    n_cat = sum(1 for r in rows if r.risk_score > 150)
    n_dan = sum(1 for r in rows if 30 < r.risk_score <= 150)
    n_saf = sum(1 for r in rows if r.risk_score <= 30)

    print(f"  {len(rows)} contingencies evaluated in {time.time()-t0:.1f}s")
    print(f"  {'Catastrophic':12s}: {_c('red', str(n_cat))}")
    print(f"  {'Dangerous':12s}: {_c('yellow', str(n_dan))}")
    print(f"  {'Safe':12s}: {_c('green', str(n_saf))}")

    print()
    print(f"  {'Rank':<5} {'Line':<8} {'Conv':5} {'Overloads':10} {'MaxLoad%':10} {'Risk':8}")
    print("  " + "─" * 50)
    for rank, row in enumerate(rows[:10], 1):
        if not row.converged or row.risk_score >= 999:
            status = _c("red", "ISLND")
        elif row.max_loading_pct >= 100:
            status = _c("red",    f"{row.max_loading_pct:6.1f}%")
        elif row.max_loading_pct >= 80:
            status = _c("yellow", f"{row.max_loading_pct:6.1f}%")
        else:
            status = _c("green",  f"{row.max_loading_pct:6.1f}%")
        risk_col = _c("red" if row.risk_score > 100 else ("yellow" if row.risk_score > 30 else "green"),
                      f"{row.risk_score:6.1f}")
        print(f"  #{rank:<4} {row.name:<8} {'Y' if row.converged else 'N':5} "
              f"{row.n_overloaded:<10} {status:18} {risk_col}")

    if args.screen_only:
        print(_c("green", "\n✓ Screening complete."))
        return

    # ── 3. select & trigger fault ─────────────────────────────────────────────
    fault_row = select_fault_line(rows, prefer=fault_pref)
    print(_c("bold", f"\n▶ Escalating fault: {fault_row.name} (risk={fault_row.risk_score})"))

    state = get_post_fault_state(net, fault_row.line_index)
    print(_c("red", f"  Post-fault max loading : {state.max_loading_pct:.1f}%"))
    print(f"  Overloaded lines       : {len(state.overloaded_lines)}")
    for ol in state.overloaded_lines:
        print(f"    {ol['name']:8s} {_bar(ol['loading_pct'])} {ol['loading_pct']:.1f}%")

    if state.max_loading_pct < 100.0:
        print(_c("green", "\n✓ No overloads after this fault — grid remains N-1 secure."))
        return

    # ── 4. build action space ─────────────────────────────────────────────────
    print(_c("cyan", "\n▶ Building corrective action space…"))
    action_space = build_action_space(net, fault_row.line_index, state)
    print(f"  {len(action_space)} feasible actions")
    for a in action_space[:8]:
        tag = " ★ RECOMMENDED" if a.recommended else ""
        print(f"  [{a.target_id:<30}] {a.label:<35} "
              f"sens={a.sensitivity:+.2f} pp/MW  "
              f"cost={a.cost_per_mw:>8,.0f} €/MWh{tag}")

    # ── 5. corrective loop ────────────────────────────────────────────────────
    print(_c("cyan", f"\n▶ Running corrective loop (max {args.max_iterations} iterations)…"))
    iterations, final_net, final_state = run_corrective_loop(
        net=net,
        fault_line_idx=fault_row.line_index,
        fault_line_name=fault_row.name,
        action_space=action_space,
        initial_state=state,
        api_key=args.api_key,
        model=args.model,
        max_iterations=args.max_iterations,
        weather=weather,
        hour=hour,
    )

    # ── 6. results ────────────────────────────────────────────────────────────
    print()
    total_cost = sum(it.cost_eur for it in iterations)
    for it in iterations:
        secure = it.overloads_after == 0
        col = "green" if secure else "yellow"
        print(_c("bold", f"  Iteration {it.iteration}"))
        print(f"  Loading : {it.max_loading_before:.1f}% → {_c(col, f'{it.max_loading_after:.1f}%')}")
        print(f"  Reasoning: {it.reasoning[:200]}")
        for cmd in it.commands:
            print(f"    ⤷ {cmd.action_id:<35}  {cmd.amount_mw:.3f} MW")
        print(f"  Iteration cost: {it.cost_eur:.0f} €/h\n")

    secure_final = final_state.max_loading_pct < 100.0
    print("─" * 52)
    if secure_final:
        print(_c("green", f"✓ N-1 SECURITY RESTORED"))
    else:
        print(_c("red", f"✗ Security NOT fully restored"))
    print(f"  Final max loading : {final_state.max_loading_pct:.1f}%")
    print(f"  Total cost        : {total_cost:,.0f} €/h")
    print(f"  Iterations        : {len(iterations)}")
    print("─" * 52)


if __name__ == "__main__":
    main()
