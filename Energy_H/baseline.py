"""Optimization baseline: LP corrective/preventive dispatch (SCOPF-lite).

Instead of greedy single-line fixes (which ping-pong when two overloads
pull the same generators in opposite directions), the baseline solves one
linear program over ALL monitored lines in the base case and in every
failing N-1 contingency:

  variables    up_i, dn_i  injection shifts: generator redispatch and
                           battery charge/discharge (bounded by headroom,
                           ratings, and 1 h of state of charge)
               shed_j      load curtailment / solar spill (bounded by the
                           element's current MW)
  objective    min  sum cost_i*(up_i+dn_i) + sum cost_j*shed_j
               ($30/MW gen, $10/MW battery, $50/MW solar, $2000/MW load)
  constraints  for every monitored line in every monitored network:
                 sign * (flow + A_inj.(up-dn) + A_shed.shed) <= limit*margin

Sensitivities A come from DC power-flow perturbation (slack absorbing),
flows and limits from the AC solution. Because the constraints are
linearized, the LP is re-screened and re-solved up to a few rounds.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import linprog

from env import (
    COST_CURTAIL_PER_MW,
    COST_REDISPATCH_PER_MW,
    COST_SOLAR_CURTAIL_PER_MW,
    COST_STORAGE_PER_MW,
    GridEnv,
)
from market import balancing_price
from network import clone, runpf

PERTURB_MW = 0.2       # DC sensitivity probe (feeder-scale, was 5 MW)
MONITOR_PCT = 75.0     # lines above this loading get a constraint
MARGIN = 0.92          # target at most 92% of the thermal limit
MAX_ROUNDS = 8
MAX_CTG_NETWORKS = 8   # cap on contingency networks per LP round


def _scale(net) -> tuple[float, float]:
    """(min_var_mw, min_action_mw) thresholds scaled to the grid size.

    Transmission cases have MW-scale loads; the IEEE 123 feeder has
    kW-scale loads (0.02-0.1 MW each), so fixed 0.5 MW cut-offs would
    discard every distribution lever. Scale to total load instead."""
    total = float(net.load.p_mw.sum())
    return max(0.003, 1e-4 * total), max(0.005, 1e-3 * total)


def _variables(net):
    """Decision variables from the current grid state.

    injs:  (kind, idx, up_bound, dn_bound, cost) — injection shifts
    sheds: (kind, idx, bound, cost)              — MW removed from service
    """
    redisp_cost = balancing_price(net, COST_REDISPATCH_PER_MW)
    injs = []
    for g in net.gen.index:
        p = float(net.gen.at[g, "p_mw"])
        injs.append((
            "gen", int(g),
            max(0.0, float(net.gen.at[g, "max_p_mw"]) - p),
            max(0.0, p - float(net.gen.at[g, "min_p_mw"])),
            redisp_cost,
        ))
    for s in net.storage.index:
        inj = -float(net.storage.at[s, "p_mw"])  # grid convention
        soc = float(net.storage.at[s, "soc_percent"]) / 100.0
        e = float(net.storage.at[s, "max_e_mwh"])
        p_max = float(net.storage.at[s, "max_p_mw"])
        up = min(p_max - inj, max(0.0, soc * e))          # discharge more
        dn = min(inj + p_max, max(0.0, (1 - soc) * e))    # charge more
        injs.append(("storage", int(s), max(0.0, up), max(0.0, dn),
                     COST_STORAGE_PER_MW))

    min_var, _ = _scale(net)
    sheds = []
    for l in net.load.index:
        p = float(net.load.at[l, "p_mw"])
        if p > min_var:
            sheds.append(("load", int(l), p, COST_CURTAIL_PER_MW))
    for sg in net.sgen.index:
        p = float(net.sgen.at[sg, "p_mw"])
        if p > min_var:
            sheds.append(("sgen", int(sg), p, COST_SOLAR_CURTAIL_PER_MW))
    return injs, sheds


def _perturb_inj(test, kind, idx, mw):
    if kind == "gen":
        test.gen.at[idx, "p_mw"] += mw
    else:  # storage: pandapower sign is opposite (positive = charging)
        test.storage.at[idx, "p_mw"] -= mw


def _perturb_shed(test, kind, idx, mw):
    if kind == "load":
        test.load.at[idx, "p_mw"] -= mw
    else:  # sgen
        test.sgen.at[idx, "p_mw"] -= mw


def _line_constraints(net, injs, sheds, monitor_pct=MONITOR_PCT):
    """AC flows/limits + DC sensitivities for the heavily loaded lines of one
    network. A coefficients are dFlow per +1 MW of each variable's action."""
    work = clone(net)
    if not runpf(work):
        return None
    in_serv = work.line.in_service.values
    monitored = [
        int(i)
        for i in work.res_line.index[in_serv]
        if work.res_line.at[i, "loading_percent"] >= monitor_pct
    ]
    if not monitored:
        return []

    flows, limits = {}, {}
    for i in monitored:
        p = float(work.res_line.at[i, "p_from_mw"])
        ld = float(work.res_line.at[i, "loading_percent"])
        if ld < 1.0:
            continue
        flows[i] = p
        limits[i] = abs(p) * 100.0 / ld  # MW proxy for the thermal limit

    if not runpf(work, dc=True):
        return None
    base_dc = {i: float(work.res_line.at[i, "p_from_mw"]) for i in flows}

    a_inj = {i: np.zeros(len(injs)) for i in flows}
    for k, (kind, idx, up, dn, _) in enumerate(injs):
        if up <= 0 and dn <= 0:
            continue
        if kind == "gen" and not bool(net.gen.at[idx, "in_service"]):
            continue
        test = clone(net)
        _perturb_inj(test, kind, idx, PERTURB_MW)
        if not runpf(test, dc=True):
            continue
        for i in flows:
            a_inj[i][k] = (
                float(test.res_line.at[i, "p_from_mw"]) - base_dc[i]
            ) / PERTURB_MW

    a_shed = {i: np.zeros(len(sheds)) for i in flows}
    for k, (kind, idx, bound, _) in enumerate(sheds):
        mw = min(PERTURB_MW, bound)
        test = clone(net)
        _perturb_shed(test, kind, idx, mw)
        if not runpf(test, dc=True):
            continue
        for i in flows:
            a_shed[i][k] = (
                float(test.res_line.at[i, "p_from_mw"]) - base_dc[i]
            ) / mw

    out = []
    for i in flows:
        sign = 1.0 if flows[i] >= 0 else -1.0
        out.append(
            {
                "sign": sign,
                "flow": flows[i],
                "limit": limits[i],
                "a_inj": a_inj[i],
                "a_shed": a_shed[i],
            }
        )
    return out


def _solve_lp(env: GridEnv, ctg_list: list[tuple[str, int]], margin=MARGIN):
    """Build and solve the LP. Returns (injs, sheds, x_inj, x_shed) or None."""
    injs, sheds = _variables(env.net)
    ni, ns = len(injs), len(sheds)

    networks = [env.net]
    for kind, el in ctg_list[:MAX_CTG_NETWORKS]:
        test = clone(env.net)
        if kind == "line":
            test.line.at[el, "in_service"] = False
        else:
            test.gen.at[el, "in_service"] = False
        networks.append(test)

    rows_A, rows_b = [], []
    for net in networks:
        cons = _line_constraints(net, injs, sheds)
        if cons is None:
            continue  # diverged network — handled by later rounds
        for c in cons:
            # sign*(flow + a_inj.(up-dn) + a_shed.shed) <= limit*margin
            a = np.concatenate(
                [c["sign"] * c["a_inj"], -c["sign"] * c["a_inj"],
                 c["sign"] * c["a_shed"]]
            )
            rows_A.append(a)
            rows_b.append(c["limit"] * margin - c["sign"] * c["flow"])

    if not rows_A:
        return injs, sheds, np.zeros(2 * ni), np.zeros(ns)

    c_obj = np.concatenate(
        [
            np.array([v[4] for v in injs]),
            np.array([v[4] for v in injs]),
            np.array([v[3] for v in sheds]) if ns else np.zeros(0),
        ]
    )
    bounds = (
        [(0, v[2]) for v in injs]
        + [(0, v[3]) for v in injs]
        + [(0, v[2]) for v in sheds]
    )
    res = linprog(
        c_obj, A_ub=np.array(rows_A), b_ub=np.array(rows_b), bounds=bounds,
        method="highs",
    )
    if not res.success:
        return None
    return injs, sheds, res.x[: 2 * ni], res.x[2 * ni :]


def _to_actions(env: GridEnv, injs, sheds, x_inj, x_shed) -> list[dict]:
    ni = len(injs)
    min_act = _scale(env.net)[1]
    actions = []
    for k, (kind, idx, _up, _dn, _c) in enumerate(injs):
        delta = float(x_inj[k] - x_inj[ni + k])
        if abs(delta) < min_act:
            continue
        if kind == "gen":
            actions.append(
                {"type": "redispatch", "gen": idx, "delta_mw": round(delta, 3)}
            )
        else:
            new_inj = -float(env.net.storage.at[idx, "p_mw"]) + delta
            actions.append(
                {"type": "set_storage", "storage": idx, "p_mw": round(new_inj, 3)}
            )
    for k, (kind, idx, bound, _c) in enumerate(sheds):
        mw = float(x_shed[k]) if len(x_shed) else 0.0
        if mw < 0.5 * min_act:
            continue
        frac = round(min(mw / bound, 1.0), 4)
        if kind == "load":
            actions.append({"type": "curtail_load", "load": idx, "fraction": frac})
        else:
            actions.append({"type": "curtail_solar", "sgen": idx, "fraction": frac})
    return actions


def _local_shed(env: GridEnv, line: int, loading: float) -> dict | None:
    """Direct relief for a line the LP cannot fix: shed load at its
    receiving end. Used when DC sensitivities underestimate a line whose
    loading is dominated by reactive flow."""
    net = env.net
    min_var = _scale(net)[0]
    frac = min(0.3, max(0.05, (loading - 90.0) / 100.0))
    buses = [int(net.line.at[line, "to_bus"]), int(net.line.at[line, "from_bus"])]
    buses.sort(key=lambda b: -float(net.load.loc[net.load.bus == b, "p_mw"].sum()))
    for bus in buses:
        cands = [
            int(l) for l in net.load.index
            if int(net.load.at[l, "bus"]) == bus and float(net.load.at[l, "p_mw"]) > min_var
        ]
        if cands:
            biggest = max(cands, key=lambda l: float(net.load.at[l, "p_mw"]))
            return {"type": "curtail_load", "load": biggest, "fraction": round(frac, 3)}
    # no load at the line's endpoints (radial feeder): shed the feeder's
    # single biggest load as a last resort
    if len(net.load):
        biggest = max(net.load.index, key=lambda l: float(net.load.at[l, "p_mw"]))
        if float(net.load.at[biggest, "p_mw"]) > min_var:
            return {"type": "curtail_load", "load": int(biggest), "fraction": round(frac, 3)}
    return None


def _stuck_lines(env: GridEnv, n1: dict | None) -> list[dict]:
    """Overloaded lines in the base case, else in failing contingencies."""
    base = env.status()["violations"]["overloaded_lines"]
    if base or not n1:
        return base
    out = []
    for fc in n1.get("failing_contingencies", []):
        if isinstance(fc["violations"], dict):
            out.extend(fc["violations"].get("overloaded_lines", []))
    return out


def run_baseline(env: GridEnv, target_n1: bool = True, verbose: bool = False) -> dict:
    """LP-based policy: reclose what we can, then iterate screen -> LP -> apply."""
    # free capacity first: reclose open, non-faulted lines if they help
    faulted = set(env.net.get("faulted_lines", []))
    for idx in env.net.line.index[~env.net.line.in_service]:
        if int(idx) in faulted:
            continue
        sim = env.simulate([{"type": "switch_line", "line": int(idx), "close": True}])
        if sim["ok"]:
            env.apply([{"type": "switch_line", "line": int(idx), "close": True}])
            if verbose:
                print(f"  [baseline] reclosed line {int(idx)}")

    n1 = None
    for rnd in range(MAX_ROUNDS):
        ctg_list: list[tuple[str, int]] = []
        if target_n1:
            n1 = env.screen()
            if n1["secure"] and env.is_base_secure():
                break
            for fc in n1["failing_contingencies"]:
                kind, el = fc["contingency"].split("-")
                ctg_list.append((kind, int(el)))
            # worst (non-converged or biggest) first; screen returns watchlist order
        elif env.is_base_secure():
            break

        sol = _solve_lp(env, ctg_list)
        actions = _to_actions(env, *sol) if sol is not None else []
        if actions:
            r = env.apply(actions)
            if verbose:
                print(
                    f"  [baseline] round {rnd+1}: LP plan with {len(actions)} "
                    f"actions, applied ok={r['ok']}"
                )
            if not r["ok"]:
                break
        # stall breaker: lines whose loading is mostly reactive flow look
        # nearly insensitive to the DC model, so the LP shaves them by
        # fractions of a MW per round — shed at the receiving bus instead
        moved = sum(
            abs(a.get("delta_mw", a.get("p_mw", 0.0))) for a in actions
        ) + 100.0 * sum(a.get("fraction", 0.0) for a in actions)
        if (rnd >= 2 and moved < 8.0) or rnd >= 4:
            stuck = _stuck_lines(env, n1)
            if stuck:
                worst = max(stuck, key=lambda o: o["loading_percent"])
                act = _local_shed(env, worst["line"], worst["loading_percent"])
                if act and env.apply([act])["ok"] and verbose:
                    print(f"  [baseline] stall breaker: {act} for line {worst['line']}")
        elif not actions:
            break

    if target_n1:
        n1 = env.screen()
    return {
        "base_secure": env.is_base_secure(),
        "n1_secure": (n1 or {}).get("secure") if target_n1 else None,
        "cost_usd": round(env.cost, 0),
        "n_actions": len(env.action_log),
        "actions": env.action_log,
    }
