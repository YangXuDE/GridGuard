"""
GridGuard — power system environment.

Ring-feeder topology (4.16 kV, ~15 buses) inspired by the IEEE 123-node
distribution test feeder.  Lines L13 / L19 / L55 are the three pre-set
N-1 fault scenarios used on the front-end.

Two-stage N-1 screening
  Stage 1  – DC power-flow pre-screen all lines (fast)
  Stage 2  – AC solve for lines whose DC risk score exceeds the threshold

Action space builder
  BESS discharge / charge, renewable curtailment, load curtailment.
  Each option is annotated with sensitivity (Δloading_pp / MW on the
  most-loaded post-fault line).
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandapower as pp


# ── network construction ───────────────────────────────────────────────────────

def create_feeder(
    load_scale: float = 1.0,
    weather: str = "clear",
    hour: int = 12,
) -> pp.pandapowerNet:
    """
    Build a ring distribution feeder with DER assets.

    Ring topology
    ─────────────
    Sub(0) ─L1─► A(1) ─L2─► B(2) ─L13─► C(3) ─L4─► D(4) ─L5─► Sub(0)

    Laterals off the ring
    ─────────────────────
    A ─L7──► Bus_47 ─L8──► Load_A          (BESS @ Bus_47)
    B ─L3──► Bus_48                         (BESS @ Bus_48)
    B ─L6──► Bus_51                         (BESS @ Bus_51)
    C ─L19─► Bus_65 ─L21─► Load_C          (BESS @ Bus_65, solar PV)
    C ─L20─► Bus_76                         (BESS @ Bus_76)
    D ─L55─► Bus_111                        (wind farm)
    A ─L9──► Bus_30 ─L30─► Load_E

    Fault scenarios
    ───────────────
    L13 trips  →  loads at C, Bus_65, Bus_76 re-routed via D→C  →  L4, L5 overloaded
    L19 trips  →  Bus_65, Load_C lose PV-backed supply  →  L2, L13 overloaded
    L55 trips  →  Bus_111 wind farm disconnected  →  D load deficit  →  L4 overloaded
    """
    net = pp.create_empty_network(name="GridGuard feeder", f_hz=60, sn_mva=1.0)

    # ── buses ─────────────────────────────────────────────────────────────────
    vn = 4.16  # kV
    def bus(name, vn_kv=vn, bus_type="b"):
        return pp.create_bus(net, vn_kv=vn_kv, name=name, type=bus_type)

    sub   = bus("Sub",     vn_kv=vn, bus_type="b")   # 0
    a     = bus("B1",      vn_kv=vn)                  # 1
    b     = bus("B2",      vn_kv=vn)                  # 2
    c     = bus("B3",      vn_kv=vn)                  # 3
    d     = bus("B4",      vn_kv=vn)                  # 4
    bus47 = bus("Bus_47",  vn_kv=vn)                  # 5
    loada = bus("Load_A",  vn_kv=vn)                  # 6
    bus48 = bus("Bus_48",  vn_kv=vn)                  # 7
    bus51 = bus("Bus_51",  vn_kv=vn)                  # 8
    bus65 = bus("Bus_65",  vn_kv=vn)                  # 9
    loadc = bus("Load_C",  vn_kv=vn)                  # 10
    bus76 = bus("Bus_76",  vn_kv=vn)                  # 11
    bus111= bus("Bus_111", vn_kv=vn)                  # 12
    bus30 = bus("Bus_30",  vn_kv=vn)                  # 13
    loade = bus("Load_E",  vn_kv=vn)                  # 14

    # ── external grid (slack) ─────────────────────────────────────────────────
    pp.create_ext_grid(net, bus=sub, vm_pu=1.02, va_degree=0, name="Substation")

    # ── line standard type (MV XLPE cable, 4.16 kV) ──────────────────────────
    # thermal limit 0.25 kA → S_max = √3 × 4.16 × 0.25 ≈ 1.80 MVA
    ltype = {
        "r_ohm_per_km": 0.411,
        "x_ohm_per_km": 0.082,
        "c_nf_per_km":  350.0,
        "max_i_ka":     0.250,
    }
    def line(fb, tb, length_km, name):
        return pp.create_line_from_parameters(
            net, from_bus=fb, to_bus=tb,
            length_km=length_km, name=name,
            **ltype,
        )

    # ring lines
    line(sub, a,  0.8, "L1")    # 0
    line(a,   b,  1.2, "L2")    # 1
    line(b,   c,  1.0, "L13")   # 2  ← heatwave fault line
    line(c,   d,  0.9, "L4")    # 3
    line(d,   sub,1.1, "L5")    # 4

    # lateral feeders
    line(a, bus47, 0.6, "L7")   # 5
    line(bus47, loada, 0.4, "L8")  # 6
    line(b, bus48, 0.5, "L3")   # 7
    line(b, bus51, 0.7, "L6")   # 8
    line(c, bus65, 0.6, "L19")  # 9  ← solar fault line
    line(bus65, loadc, 0.4, "L21") # 10
    line(c, bus76, 0.8, "L20")  # 11
    line(d, bus111, 0.9, "L55") # 12 ← storm fault line
    line(a, bus30, 0.5, "L9")   # 13
    line(bus30, loade, 0.6, "L30") # 14

    # ── loads ─────────────────────────────────────────────────────────────────
    # base loads (MW, lagging pf=0.92)
    base_loads = [
        (a,     0.30), (b,     0.45), (c,     0.55), (d,     0.40),
        (loada, 0.60), (bus48, 0.35), (bus51, 0.25),
        (loadc, 0.55), (bus76, 0.30), (bus111,0.20),
        (bus30, 0.15), (loade, 0.50),
    ]
    for bus_idx, p_mw in base_loads:
        pp.create_load(net, bus=bus_idx, p_mw=p_mw * load_scale,
                       q_mvar=p_mw * load_scale * 0.43,  # pf≈0.92
                       name=f"Load@{net.bus.at[bus_idx,'name']}")

    # ── DER: solar PV ─────────────────────────────────────────────────────────
    solar_out = _solar_output(weather, hour)   # 0..1 fraction
    solar_sites = [(bus65, 1.8), (loada, 0.9), (loadc, 1.2)]
    for bus_idx, rated_mw in solar_sites:
        p = rated_mw * solar_out
        if p > 0.01:
            pp.create_sgen(net, bus=bus_idx, p_mw=p, q_mvar=0,
                           name=f"PV@{net.bus.at[bus_idx,'name']}",
                           type="PV")

    # ── DER: wind ─────────────────────────────────────────────────────────────
    wind_out = _wind_output(weather, hour)
    pp.create_sgen(net, bus=bus111, p_mw=2.5 * wind_out, q_mvar=0,
                   name="WindFarm@Bus_111", type="WP")

    # ── DER: BESS (modelled as sgen for discharge, load for charge) ───────────
    bess_sites = [
        ("Bus_47", bus47,  0.39, 60.0),
        ("Bus_48", bus48,  0.39, 60.0),
        ("Bus_51", bus51,  0.39, 60.0),
        ("Bus_65", bus65,  0.39, 60.0),
        ("Bus_76", bus76,  0.39, 60.0),
    ]
    net["_bess"] = []
    for bname, bus_idx, p_mw, soc_pct in bess_sites:
        net["_bess"].append({
            "name": bname, "bus": bus_idx,
            "bus_name": net.bus.at[bus_idx, "name"],
            "max_p_mw": p_mw, "max_e_mwh": round(p_mw * 2.5, 2),
            "soc_pct": soc_pct,
        })

    return net


def _solar_output(weather: str, hour: int) -> float:
    base = max(0.0, math.sin(math.pi * (hour - 6) / 12)) if 6 <= hour <= 18 else 0.0
    mult = {"heatwave": 0.85, "clear": 1.0, "storm": 0.15}.get(weather, 0.7)
    return base * mult


def _wind_output(weather: str, hour: int) -> float:
    return {"heatwave": 0.12, "clear": 0.35, "storm": 0.90}.get(weather, 0.35)


# ── two-stage N-1 screening ────────────────────────────────────────────────────

@dataclass
class ScreeningRow:
    line_index: int
    name: str
    converged: bool
    n_overloaded: int
    max_loading_pct: float
    risk_score: float
    island_kw: float = 0.0


def screen_all_contingencies(net: pp.pandapowerNet) -> list[ScreeningRow]:
    """Two-stage N-1 screen: DC pre-filter → AC solve shortlist."""
    # base-case AC (need converged base for DC estimates)
    pp.runpp(net, algorithm="nr", numba=False)

    dc_net = copy.deepcopy(net)
    try:
        pp.rundcpp(dc_net, numba=False)
        dc_loading = dc_net.res_line["loading_percent"].values.copy()
    except Exception:
        dc_loading = net.res_line["loading_percent"].values.copy()

    rows: list[ScreeningRow] = []
    n_lines = len(net.line)

    for i in range(n_lines):
        lname = net.line.at[i, "name"]
        # Stage 1: DC estimate of N-1 loading (rough approximation)
        dc_approx = _dc_n1_estimate(dc_loading, i)
        if dc_approx < 50.0:
            # safe by DC screen — skip AC solve
            rows.append(ScreeningRow(
                line_index=i, name=lname, converged=True,
                n_overloaded=0, max_loading_pct=dc_approx,
                risk_score=max(0.0, dc_approx - 50.0) * 0.3,
            ))
            continue

        # Stage 2: full AC N-1 solve
        row = _ac_n1_solve(net, i)
        rows.append(row)

    rows.sort(key=lambda r: -r.risk_score)
    return rows


def _dc_n1_estimate(base_loading: np.ndarray, tripped: int) -> float:
    """Heuristic: redistribute tripped line's MW to adjacent lines (±40%)."""
    est = base_loading.copy()
    delta = base_loading[tripped]
    for j in range(len(est)):
        if j != tripped:
            est[j] += delta * 0.35
    return float(est[np.arange(len(est)) != tripped].max())


def _ac_n1_solve(net: pp.pandapowerNet, tripped_idx: int) -> ScreeningRow:
    """AC power-flow with line tripped. Returns ScreeningRow."""
    lname = net.line.at[tripped_idx, "name"]
    test = copy.deepcopy(net)
    test.line.at[tripped_idx, "in_service"] = False

    try:
        pp.runpp(test, algorithm="nr", numba=False,
                 max_iteration=50, tolerance_mva=1e-4)
    except pp.powerflow.LoadflowNotConverged:
        return ScreeningRow(
            line_index=tripped_idx, name=lname,
            converged=False, n_overloaded=0,
            max_loading_pct=0.0, risk_score=999.0, island_kw=999.0,
        )

    loading = test.res_line["loading_percent"]
    overloaded = loading[loading > 100.0]
    n_over = len(overloaded)
    max_load = float(loading.max())
    v_pu = test.res_bus["vm_pu"]
    n_undervolt = int((v_pu < 0.95).sum())

    # risk score = severity × likelihood proxy
    risk = (
        n_over * 15.0
        + max(0.0, max_load - 100.0) * 1.2
        + n_undervolt * 8.0
    )
    return ScreeningRow(
        line_index=tripped_idx, name=lname, converged=True,
        n_overloaded=n_over, max_loading_pct=round(max_load, 2),
        risk_score=round(risk, 1),
    )


def select_fault_line(rows: list[ScreeningRow], prefer: Optional[str] = None) -> ScreeningRow:
    """Return the highest-risk converged contingency (or a named one)."""
    if prefer:
        for r in rows:
            if r.name == prefer:
                return r
    for r in sorted(rows, key=lambda r: -r.risk_score):
        if r.converged and r.risk_score < 999:
            return r
    return rows[0]


# ── post-fault state ───────────────────────────────────────────────────────────

@dataclass
class PostFaultState:
    max_loading_pct: float
    overloaded_lines: list[dict]   # {name, loading_pct, from_bus, to_bus}
    undervolt_buses: int
    total_load_mw: float


def get_post_fault_state(net: pp.pandapowerNet, fault_line_idx: int) -> PostFaultState:
    """Run AC power-flow with fault line tripped; return network state."""
    fnet = copy.deepcopy(net)
    fnet.line.at[fault_line_idx, "in_service"] = False
    try:
        pp.runpp(fnet, algorithm="nr", numba=False,
                 max_iteration=50, tolerance_mva=1e-4)
    except pp.powerflow.LoadflowNotConverged:
        return PostFaultState(
            max_loading_pct=999.0, overloaded_lines=[],
            undervolt_buses=len(fnet.bus), total_load_mw=0.0,
        )

    loading = fnet.res_line["loading_percent"]
    overloaded = []
    for idx, row in fnet.line.iterrows():
        if idx == fault_line_idx:
            continue
        pct = float(loading.at[idx])
        if pct > 100.0:
            overloaded.append({
                "name": row["name"],
                "loading_pct": round(pct, 1),
                "from_bus": net.bus.at[row["from_bus"], "name"],
                "to_bus":   net.bus.at[row["to_bus"],  "name"],
            })

    v_pu = fnet.res_bus["vm_pu"]
    n_under = int((v_pu < 0.95).sum())
    total_p = float(fnet.res_load["p_mw"].sum()) if len(fnet.res_load) else 0.0

    return PostFaultState(
        max_loading_pct=round(float(loading.max()), 2),
        overloaded_lines=sorted(overloaded, key=lambda x: -x["loading_pct"]),
        undervolt_buses=n_under,
        total_load_mw=round(total_p, 3),
    )


# ── action space ──────────────────────────────────────────────────────────────

@dataclass
class ActionOption:
    target_id: str          # unique key
    bus_name: str
    label: str
    action_type: str        # discharge_battery | charge_battery | curtail_renewable | curtail_load
    max_available_mw: float
    cost_per_mw: float      # €/MWh
    sensitivity: float      # Δloading_pp / MW on worst overloaded line (negative = relief)
    target_line: str        # line most affected by this action
    soc_pct: Optional[float] = None
    recommended: bool = False


def build_action_space(
    net: pp.pandapowerNet,
    fault_line_idx: int,
    state: PostFaultState,
) -> list[ActionOption]:
    """
    Build feasible corrective actions and compute sensitivities via
    finite-difference power-flow (±0.1 MW perturbation).
    """
    if not state.overloaded_lines:
        return []

    target_line_name = state.overloaded_lines[0]["name"]
    target_line_idx = net.line[net.line["name"] == target_line_name].index[0]

    def sensitivity(fnet_base: pp.pandapowerNet, bus_idx: int,
                    delta_mw: float, is_gen: bool) -> float:
        """Δloading_pp on target line for +delta_mw at bus_idx."""
        fnet = copy.deepcopy(fnet_base)
        if is_gen:
            # increase generator output
            mask = fnet.sgen["bus"] == bus_idx
            if mask.any():
                fnet.sgen.loc[mask, "p_mw"] += delta_mw
            else:
                pp.create_sgen(fnet, bus=bus_idx, p_mw=delta_mw, q_mvar=0)
        else:
            # reduce load
            mask = fnet.load["bus"] == bus_idx
            if mask.any():
                fnet.load.loc[mask, "p_mw"] -= delta_mw
        try:
            pp.runpp(fnet, algorithm="nr", numba=False,
                     max_iteration=50, tolerance_mva=1e-4)
            return float(fnet.res_line.at[target_line_idx, "loading_percent"])
        except Exception:
            return float("nan")

    # base loading on target line (post-fault)
    fnet_base = copy.deepcopy(net)
    fnet_base.line.at[fault_line_idx, "in_service"] = False
    pp.runpp(fnet_base, algorithm="nr", numba=False,
             max_iteration=50, tolerance_mva=1e-4)
    base_loading = float(fnet_base.res_line.at[target_line_idx, "loading_percent"])

    delta = 0.10  # MW perturbation for finite difference
    options: list[ActionOption] = []

    # 1. BESS discharge
    for bess in net.get("_bess", []):
        bus_idx = bess["bus"]
        avail = round(bess["max_p_mw"] * bess["soc_pct"] / 100, 3)
        if avail < 0.01:
            continue
        new_load = sensitivity(fnet_base, bus_idx, delta, is_gen=True)
        if math.isnan(new_load):
            continue
        sens = round((new_load - base_loading) / delta, 3)
        options.append(ActionOption(
            target_id=f"bess_dis_{bess['name']}",
            bus_name=bess["bus_name"],
            label=f"BESS @ {bess['bus_name']} discharge",
            action_type="discharge_battery",
            max_available_mw=avail,
            cost_per_mw=10.0,
            sensitivity=sens,
            target_line=target_line_name,
            soc_pct=bess["soc_pct"],
        ))

    # 2. Renewable curtailment
    for idx, sgen_row in net.sgen.iterrows():
        p_now = float(sgen_row["p_mw"])
        if p_now < 0.05:
            continue
        bus_idx = int(sgen_row["bus"])
        bus_name = net.bus.at[bus_idx, "name"]
        new_load = sensitivity(fnet_base, bus_idx, -delta, is_gen=True)
        if math.isnan(new_load):
            continue
        sens = round((new_load - base_loading) / (-delta), 3)
        cost = 22.0 if sgen_row.get("type") == "WP" else 15.0
        options.append(ActionOption(
            target_id=f"curtail_res_{idx}",
            bus_name=bus_name,
            label=f"{sgen_row.get('name','RES')} curtail",
            action_type="curtail_renewable",
            max_available_mw=round(p_now, 3),
            cost_per_mw=cost,
            sensitivity=sens,
            target_line=target_line_name,
        ))

    # 3. Load curtailment
    for idx, load_row in net.load.iterrows():
        p_now = float(load_row["p_mw"])
        if p_now < 0.05:
            continue
        bus_idx = int(load_row["bus"])
        bus_name = net.bus.at[bus_idx, "name"]
        new_load = sensitivity(fnet_base, bus_idx, -delta, is_gen=False)
        if math.isnan(new_load):
            continue
        sens = round((new_load - base_loading) / (-delta), 3)
        is_critical = "critical" in str(load_row.get("name", "")).lower()
        cost = 100_000.0 if is_critical else 10_000.0
        options.append(ActionOption(
            target_id=f"curtail_load_{idx}",
            bus_name=bus_name,
            label=f"Load @ {bus_name}",
            action_type="curtail_load",
            max_available_mw=round(p_now, 3),
            cost_per_mw=cost,
            sensitivity=sens,
            target_line=target_line_name,
        ))

    # filter: only actions that provide relief
    relieving = [o for o in options if o.sensitivity < 0]
    if not relieving:
        relieving = options  # fallback

    # mark recommended: best |sensitivity| / cost
    if relieving:
        best = max(relieving, key=lambda o: abs(o.sensitivity) / max(o.cost_per_mw, 1))
        best.recommended = True

    return sorted(relieving, key=lambda o: o.cost_per_mw)


# ── apply corrective action ────────────────────────────────────────────────────

def apply_action(
    net: pp.pandapowerNet,
    fault_line_idx: int,
    action: ActionOption,
    amount_mw: float,
) -> tuple[pp.pandapowerNet, PostFaultState]:
    """Apply one corrective action and return updated (net, state)."""
    new_net = copy.deepcopy(net)
    new_net.line.at[fault_line_idx, "in_service"] = False

    if action.action_type == "discharge_battery":
        bus_idx = new_net.sgen[new_net.sgen["bus"] ==
                               _bus_idx(new_net, action.bus_name)].index
        if len(bus_idx):
            new_net.sgen.loc[bus_idx[0], "p_mw"] += amount_mw
        else:
            pp.create_sgen(new_net, bus=_bus_idx(new_net, action.bus_name),
                           p_mw=amount_mw, q_mvar=0, name=f"BESS_dis_{action.bus_name}")

    elif action.action_type == "charge_battery":
        bidx = _bus_idx(new_net, action.bus_name)
        mask = new_net.load["bus"] == bidx
        if mask.any():
            new_net.load.loc[mask.idxmax(), "p_mw"] += amount_mw

    elif action.action_type == "curtail_renewable":
        idx_str = action.target_id.replace("curtail_res_", "")
        try:
            sgen_idx = int(idx_str)
            cur = float(new_net.sgen.at[sgen_idx, "p_mw"])
            new_net.sgen.at[sgen_idx, "p_mw"] = max(0.0, cur - amount_mw)
        except (ValueError, KeyError):
            pass

    elif action.action_type == "curtail_load":
        idx_str = action.target_id.replace("curtail_load_", "")
        try:
            load_idx = int(idx_str)
            cur = float(new_net.load.at[load_idx, "p_mw"])
            new_net.load.at[load_idx, "p_mw"] = max(0.0, cur - amount_mw)
        except (ValueError, KeyError):
            pass

    new_state = get_post_fault_state(new_net, fault_line_idx)
    return new_net, new_state


def _bus_idx(net: pp.pandapowerNet, bus_name: str) -> int:
    match = net.bus[net.bus["name"] == bus_name]
    if len(match):
        return int(match.index[0])
    raise ValueError(f"Bus '{bus_name}' not found in network")
