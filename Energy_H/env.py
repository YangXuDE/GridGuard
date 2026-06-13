"""Adapter module — GridEnv wrapper bridging GridEnvironment to baseline.py.

Exposes the SCOPF-lite baseline API (the same surface GridGuard's
``run_baseline`` expects) on top of our ``GridEnvironment`` physics
engine, so the LP optimisation baseline can drive the IEEE 123 feeder
with the full lever set: generator redispatch, battery charge/discharge
(state-of-charge bounded), renewable spill and load curtailment.

Action vocabulary (baseline format):
    {"type": "switch_line",  "line": i, "close": bool}
    {"type": "redispatch",   "gen": g,  "delta_mw": float}
    {"type": "set_storage",  "storage": s, "p_mw": float}   # +=discharge
    {"type": "curtail_solar","sgen": sg, "fraction": float}
    {"type": "curtail_load", "load": l,  "fraction": float}
"""

import copy
from typing import Any, Dict, List

import pandapower as pp

from grid_environment import GridEnvironment
from market import balancing_price

# Cost model (EUR/MWh) — GridGuard parity. Redispatch is overridden at the
# live balancing price when a weather/market forecast is attached.
COST_REDISPATCH_PER_MW = 30.0
COST_STORAGE_PER_MW = 10.0
COST_SOLAR_CURTAIL_PER_MW = 50.0
COST_CURTAIL_PER_MW = 10_000.0
CRITICAL_CURTAIL_PER_MW = 100_000.0
CRITICAL_BUS_IDS = (2,)
OVERLOAD_PCT = 100.0


class GridEnv:
    """SCOPF-lite baseline driver over a ``GridEnvironment``."""

    def __init__(self, grid_env: GridEnvironment) -> None:
        self._env = grid_env
        self.net = grid_env.net  # raw pandapower net

        # baseline reads net.get("faulted_lines") / net.get("asset_health")
        if not hasattr(self.net, "get"):
            self.net.get = lambda key, default=None: getattr(self.net, key, default)
        # lines that may not be reclosed: the triggered N-1 fault + any
        # asset-health maintenance outage
        faulted = set()
        ah = self.net.get("asset_health") or {}
        faulted |= set(ah.get("maintenance_lines", []))
        # any line already out of service when the baseline starts is the
        # contingency we are responding to — do not reclose it
        for i in self.net.line.index[~self.net.line.in_service]:
            faulted.add(int(i))
        self.net["faulted_lines"] = faulted

        self.cost = 0.0
        self.action_log: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # observations
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Grid snapshot in GridGuard format (only the keys the baseline
        and its stall-breaker read)."""
        state = self._env.get_grid_state()
        overloaded = [
            {"line": ol.line_index,
             "from_bus": ol.from_bus, "to_bus": ol.to_bus,
             "loading_percent": ol.loading_percent}
            for ol in state.overloaded_lines
        ]
        return {"violations": {"overloaded_lines": overloaded,
                               "voltage": state.voltage_violations,
                               "secure": state.converged and not overloaded
                               and not state.voltage_violations}}

    def is_base_secure(self) -> bool:
        state = self._env.get_grid_state()
        return (state.converged and not state.overloaded_lines
                and not state.voltage_violations)

    def screen(self) -> dict:
        """Two-stage N-1 screen in GridGuard's baseline format."""
        results = self._env.screen_all_contingencies()
        failing = []
        for r in results:
            if r.get("islands"):
                continue  # islanding needs restoration switching, not the LP
            if r["n_overloaded"] > 0 or not r["converged"]:
                # reconstruct the overloaded-line detail for this outage
                viol = self._contingency_violations(r["line_index"])
                failing.append({
                    "contingency": f"line-{r['line_index']}",
                    "converged": r["converged"],
                    "violations": viol,
                })
        secure = not failing and self.is_base_secure()
        return {"secure": secure, "failing_contingencies": failing}

    def _contingency_violations(self, line_idx: int) -> dict:
        """AC-solve one outage and return its overloaded lines."""
        test = copy.deepcopy(self.net)
        test.line.at[line_idx, "in_service"] = False
        try:
            pp.runpp(test, numba=False)
        except pp.LoadflowNotConverged:
            return "power flow diverged"
        over = []
        for i in test.res_line.index:
            lp = float(test.res_line.at[i, "loading_percent"])
            if lp == lp and lp > OVERLOAD_PCT:
                over.append({"line": int(i),
                             "from_bus": int(test.line.at[i, "from_bus"]),
                             "to_bus": int(test.line.at[i, "to_bus"]),
                             "loading_percent": round(lp, 1)})
        return {"overloaded_lines": over, "voltage": [], "secure": not over}

    # ------------------------------------------------------------------
    # what-if / commit
    # ------------------------------------------------------------------

    def simulate(self, actions: List[Dict[str, Any]]) -> dict:
        try:
            test = copy.deepcopy(self.net)
            self._apply_to_net(test, actions)
            pp.runpp(test, numba=False)
            return {"ok": True}
        except Exception:
            return {"ok": False}

    def apply(self, actions: List[Dict[str, Any]]) -> dict:
        try:
            self._track_cost(actions)          # cost from PRE-apply net state
            self._apply_to_net(self.net, actions)
            self._env.run_powerflow()
            self.action_log.extend(actions)
            return {"ok": True}
        except Exception:
            return {"ok": False}

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _apply_to_net(self, net, actions: List[Dict[str, Any]]) -> None:
        for act in actions:
            atype = act.get("type", "")
            if atype == "switch_line":
                lidx = int(act["line"])
                if act.get("close", False) and lidx in net.get("faulted_lines", set()):
                    continue  # faulted / maintenance line cannot reclose
                net.line.at[lidx, "in_service"] = bool(act.get("close", False))
            elif atype == "redispatch":
                g = int(act["gen"])
                delta = float(act["delta_mw"])
                cur = float(net.gen.at[g, "p_mw"])
                hi = float(net.gen.at[g, "max_p_mw"])
                lo = float(net.gen.iloc[g].get("min_p_mw", 0.0) or 0.0)
                net.gen.at[g, "p_mw"] = max(lo, min(hi, cur + delta))
            elif atype == "set_storage":
                s = int(act["storage"])
                p_grid = float(act["p_mw"])           # + = discharge to grid
                p_pp = -p_grid                         # pp: + = charging
                lo = float(net.storage.at[s, "min_p_mw"])
                hi = float(net.storage.at[s, "max_p_mw"])
                net.storage.at[s, "p_mw"] = max(lo, min(hi, p_pp))
            elif atype == "curtail_solar":
                sg = int(act["sgen"])
                frac = float(act["fraction"])
                net.sgen.at[sg, "p_mw"] = float(net.sgen.at[sg, "p_mw"]) * (1.0 - frac)
            elif atype == "curtail_load":
                l = int(act["load"])
                frac = float(act["fraction"])
                cur = float(net.load.at[l, "p_mw"])
                net.load.at[l, "p_mw"] = max(0.0, cur * (1.0 - frac))
                net.load.at[l, "q_mvar"] = float(net.load.at[l, "q_mvar"]) * (1.0 - frac)
            # unknown types silently ignored

    def _track_cost(self, actions: List[Dict[str, Any]]) -> None:
        """Accumulate cost from the net state BEFORE the actions apply."""
        redisp = balancing_price(self.net, COST_REDISPATCH_PER_MW)
        for act in actions:
            atype = act.get("type", "")
            if atype == "redispatch":
                self.cost += abs(float(act.get("delta_mw", 0.0))) * redisp
            elif atype == "set_storage":
                s = int(act["storage"])
                prev_grid = -float(self.net.storage.at[s, "p_mw"])
                self.cost += abs(float(act["p_mw"]) - prev_grid) * COST_STORAGE_PER_MW
            elif atype == "curtail_solar":
                sg = int(act["sgen"])
                spilled = float(self.net.sgen.at[sg, "p_mw"]) * float(act["fraction"])
                self.cost += spilled * COST_SOLAR_CURTAIL_PER_MW
            elif atype == "curtail_load":
                l = int(act["load"])
                shed = float(self.net.load.at[l, "p_mw"]) * float(act["fraction"])
                bus = int(self.net.load.at[l, "bus"])
                rate = (CRITICAL_CURTAIL_PER_MW if bus in CRITICAL_BUS_IDS
                        else COST_CURTAIL_PER_MW)
                self.cost += shed * rate
            elif atype == "switch_line":
                self.cost += 100.0
