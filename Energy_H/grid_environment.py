"""
grid_environment.py — Physics Engine Module

Encapsulates all pandapower operations for grid modeling, N-1 contingency
simulation, power flow execution, state observation, and action execution.

Author: Energy x AI Hackathon — E.ON Grid Operation Agents Track
"""

import copy
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pandapower as pp
import pandapower.networks as pp_nets

from ieee123 import build_ieee123
from market import balancing_price
from scenario import prepare_scenario
from weather import apply_weather


# ---------------------------------------------------------------------------
# Data classes for structured grid observations
# ---------------------------------------------------------------------------

@dataclass
class LineState:
    """Snapshot of a single transmission line after power flow."""
    line_index: int
    name: Optional[str]
    from_bus: int
    to_bus: int
    loading_percent: float
    in_service: bool


@dataclass
class GridStateReport:
    """Structured grid state returned to the agent after each power flow."""
    converged: bool
    total_lines: int
    overloaded_lines: List[LineState]
    nominal_lines: int
    max_loading_percent: float
    summary: str
    voltage_violations: List[Dict[str, Any]] = field(default_factory=list)
    weather_now: Optional[Dict[str, Any]] = None


@dataclass
class ActionOption:
    """A single action that the LLM may select from the action space."""
    target_id: Any            # int (gen/load) or str (storage_N)
    action_type: str          # 'redispatch_up', 'redispatch_down',
                              # 'curtail_load', 'discharge_battery'
    node_bus: int
    current_p_mw: float
    max_available_mw: float
    cost_per_mw: float        # EUR/MWh
    sensitivity: str           # e.g. "~0.8 MW relief on Line 0 per MW"
    description: str


# ---------------------------------------------------------------------------
# GridEnvironment
# ---------------------------------------------------------------------------

class GridEnvironment:
    """
    Physics-engine wrapper around pandapower.

    Responsibilities:
      - Dynamically load any pandapower network by name.
      - Simulate N-1 line contingencies.
      - Run AC power flow with convergence handling.
      - Report overloaded lines in a structured format.
      - Generate a constrained JSON action space for the LLM.
      - Execute LLM-issued redispatch / curtailment actions.
    """

    # Market parameters (EUR/MWh)
    DEFAULT_REDISPATCH_COST_RANGE: Tuple[float, float] = (60.0, 150.0)
    DEFAULT_LOAD_CURTAILMENT_COST: float = 10_000.0
    CRITICAL_LOAD_COST: float = 100_000.0
    CRITICAL_BUS_IDS: Tuple[int, ...] = (2,)
    OVERLOAD_THRESHOLD_PCT: float = 100.0

    def __init__(
        self,
        network_name: str = "ieee123",
        line_rating_factor: float = 1.0,
        fixed_max_i_ka: Optional[float] = None,
        load_scale: float = 1.0,
        weather: Optional[str] = "clear",
        weather_hour: int = 14,
        with_der: bool = True,
        with_asset_health: bool = True,
        seed: int = 7,
    ) -> None:
        """
        Load a pandapower network by name with authentic physics.

        Args:
            network_name: 'ieee123' (the IEEE 123-node distribution test
                          feeder, built from the official OpenDSS files)
                          or any pandapower.networks short name
                          (e.g. 'case118', 'case300', 'case1354pegase').
            line_rating_factor: Multiplier applied to all line max_i_ka.
                                Use <1.0 to derate lines for conservative
                                security assessment.
            fixed_max_i_ka: If set, override ALL line max_i_ka to this
                            value (in kA).  Use when you need a uniform
                            thermal rating baseline.
            load_scale: Multiplier on every load (stress knob).
            weather: Forecast profile ('clear', 'heatwave', 'overcast',
                     'storm') driving solar irradiance, wind speed and
                     temperature-dependent demand. None disables weather.
            weather_hour: Hour of day the scenario starts at.
            with_der: Add rooftop PV, utility PV, wind farms and BESS
                      sized to the grid's total load.
            with_asset_health: Derate two lines per the asset-health
                               register and take one line out for
                               scheduled maintenance.
        """
        self.network_name = network_name
        self.net = self._load_network(network_name)

        # Mesh the network: close all normally-open switches.
        # Critical for radial test feeders (e.g. case33bw) but harmless
        # on already-meshed networks (case118 has zero switches).
        if hasattr(self.net, "switch") and len(self.net.switch) > 0:
            opened_before = int((~self.net.switch.closed).sum())
            self.net.switch.closed = True
            print(
                f"[GridEnvironment] Meshed network: "
                f"{opened_before} previously open switch(es) now closed."
            )
        # IEEE 123 models its normally-open tie switches as out-of-service
        # lines — closing them meshes the radial backbone the same way
        ties = self.net.get("tie_lines") if hasattr(self.net, "get") else None
        if ties:
            for t in ties:
                self.net.line.at[t, "in_service"] = True
            print(
                f"[GridEnvironment] Meshed feeder: closed "
                f"{len(ties)} normally-open tie switch(es) "
                f"({', '.join(str(self.net.line.at[t, 'name']) for t in ties)})."
            )

        # Apply line rating adjustments (optional)
        if fixed_max_i_ka is not None:
            self.net.line["max_i_ka"] = float(fixed_max_i_ka)
            print(
                f"[GridEnvironment] Fixed line rating applied: "
                f"max_i_ka = {fixed_max_i_ka:.3f} kA on all lines"
            )
        elif line_rating_factor != 1.0:
            self.net.line["max_i_ka"] *= line_rating_factor
            print(
                f"[GridEnvironment] Line rating factor applied: "
                f"{line_rating_factor:.2f}x "
                f"(max_i_ka scaled)"
            )

        # Scenario pipeline (ported from GridGuard): scale load, attach
        # weather + market forecast, place DER (rooftop/utility PV, wind
        # farms, BESS sized to the grid) and the asset-health register.
        if with_der or weather or with_asset_health or load_scale != 1.0:
            prepare_scenario(
                self.net,
                load_scale=load_scale,
                weather=weather,
                weather_hour=weather_hour,
                seed=seed,
                with_der=with_der,
                with_asset_health=with_asset_health,
            )
        if not with_der and not len(self.net.storage):
            # legacy VPP BESS placement (fixed buses, transmission cases)
            for bus in (50, 80, 100):
                if bus in self.net.bus.index:
                    pp.create_storage(
                        self.net, bus=bus, p_mw=0.0, max_e_mwh=5.0,
                        max_p_mw=2.0, min_p_mw=-2.0, soc_percent=60.0,
                        name=f"VPP_BESS_{bus}",
                    )
        self._storage_bus_ids = tuple(int(b) for b in self.net.storage.bus)

        self.initial_net = copy.deepcopy(self.net)
        self.random = random.Random(42)  # seeded for reproducibility
        self._last_pf_converged: bool = False
        # sensitivity probe size: 2 % of load, sane on a 3.5 MW feeder
        # and capped at 10 MW on transmission cases
        total_load = float(self.net.load.p_mw.sum())
        self._delta_mw = min(10.0, max(0.5, round(0.02 * total_load, 2)))

        # Network summary
        n_pv = int((self.net.sgen.type != "WT").sum()) if len(self.net.sgen) else 0
        n_wt = int((self.net.sgen.type == "WT").sum()) if len(self.net.sgen) else 0
        print(
            f"[GridEnvironment] Loaded '{network_name}': "
            f"{len(self.net.bus)} buses, {len(self.net.line)} lines, "
            f"{len(self.net.gen)} gens, {len(self.net.load)} loads, "
            f"{n_pv} PV + {n_wt} wind units, "
            f"{len(self.net.storage)} BESS, "
            f"{float(self.net.load.p_mw.sum()):.2f} MW total load"
        )
        wx = self.net.get("weather")
        if wx:
            now = wx["forecast"][0]
            print(
                f"[GridEnvironment] Weather: {wx['profile']} @ "
                f"{wx['hour']:02d}:00 — {now['temp_c']}°C, "
                f"{now['wind_ms']} m/s, {now['irradiance_wm2']} W/m², "
                f"demand x{now['load_factor']}, "
                f"balancing {now['balancing_price']} EUR/MWh"
            )

    @staticmethod
    def _load_network(name: str) -> Any:
        """Load the IEEE 123 feeder or a pandapower network by name."""
        if name.lower() in ("ieee123", "case123", "ieee_123"):
            return build_ieee123()
        loader = getattr(pp_nets, name, None)
        if loader is None:
            available = ["ieee123"] + [
                n for n in dir(pp_nets) if n.startswith("case")
            ]
            raise ValueError(
                f"Unknown network '{name}'. Available: {available}"
            )
        return loader()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def trigger_n_1_fault(self, line_index: int) -> None:
        """
        Simulate an N-1 contingency by taking the specified line out of service.

        Args:
            line_index: Integer index into net.line.
        """
        if line_index < 0 or line_index >= len(self.net.line):
            raise IndexError(
                f"line_index {line_index} out of range "
                f"[0, {len(self.net.line) - 1}]"
            )
        self.net.line.at[line_index, "in_service"] = False
        line_name = self.net.line.at[line_index, "name"]
        print(
            f"[GridEnvironment] N-1 fault triggered: "
            f"Line {line_index} ('{line_name}') set out of service."
        )

    # DC screening threshold: contingencies whose worst DC loading exceeds
    # this (or that do not converge in DC) get a full AC solve.
    DC_SCREEN_PCT: float = 90.0

    def _islanded_load_kw(self, net_copy) -> Tuple[int, float]:
        """Buses left without a path to the slack and the load stranded
        on them (the radial-lateral case on distribution feeders)."""
        import pandapower.topology as top

        try:
            unsupplied = top.unsupplied_buses(net_copy)
        except Exception:
            return 0, 0.0
        if not unsupplied:
            return 0, 0.0
        stranded = float(
            net_copy.load.loc[net_copy.load.bus.isin(unsupplied), "p_mw"].sum()
        ) * 1000.0
        return len(unsupplied), stranded

    def screen_all_contingencies(self) -> List[Dict[str, Any]]:
        """
        Two-stage N-1 risk screen (ported from GridGuard).

        Stage 1 — DC power flow for EVERY in-service line outage (fast,
        linear). Stage 2 — full AC power flow only for the dangerous
        shortlist (worst DC loading >= DC_SCREEN_PCT or DC divergence).
        This is the "screen everything, solve only the scary ones"
        pattern: on the IEEE 123 feeder it cuts ~130 AC solves down to a
        handful.

        Radial laterals are special-cased: tripping them cannot overload
        the rest of the grid but it ISLANDS customers. They are reported
        with 'islands': True and a risk score equal to the unserved kW
        (in 100-kW units) instead of being power-flow solved.

        Returns:
            List of dicts sorted by risk_score descending. Keys:
              line_index, name, n_overloaded, max_loading_pct,
              risk_score, converged, stage ('dc'|'ac'|'island'),
              and for islanding outages: islands=True, unserved_kw.
        """
        import copy as _copy

        results: List[Dict[str, Any]] = []
        shortlist: List[int] = []

        # --- stage 1: DC screen (laterals short-circuited to 'island') ----
        for line_idx in self.net.line.index[self.net.line.in_service]:
            line_idx = int(line_idx)
            name = self.net.line.at[line_idx, "name"]

            net_copy = _copy.deepcopy(self.net)
            net_copy.line.at[line_idx, "in_service"] = False

            n_islanded, unserved = self._islanded_load_kw(net_copy)
            if n_islanded:
                # distribution lateral: outage = loss of supply downstream
                results.append({
                    "line_index": line_idx,
                    "name": name,
                    "n_overloaded": 0,
                    "max_loading_pct": 0.0,
                    "risk_score": round(unserved / 100.0, 2),
                    "converged": True,
                    "stage": "island",
                    "islands": n_islanded,
                    "unserved_kw": round(unserved, 1),
                })
                continue

            try:
                pp.rundcpp(net_copy)
                dc_max = float(
                    net_copy.res_line.loc[
                        net_copy.line.in_service.values, "loading_percent"
                    ].max()
                )
                dc_ok = dc_max == dc_max  # NaN guard
            except (pp.LoadflowNotConverged, Exception):
                dc_ok, dc_max = False, float("inf")

            if not dc_ok or dc_max >= self.DC_SCREEN_PCT:
                shortlist.append(line_idx)
            else:
                results.append({
                    "line_index": line_idx,
                    "name": name,
                    "n_overloaded": 0,
                    "max_loading_pct": round(dc_max, 2),
                    "risk_score": 0.0,
                    "converged": True,
                    "stage": "dc",
                })

        # --- stage 2: AC solve only the dangerous shortlist ----------------
        for line_idx in shortlist:
            name = self.net.line.at[line_idx, "name"]
            net_copy = _copy.deepcopy(self.net)
            net_copy.line.at[line_idx, "in_service"] = False
            try:
                pp.runpp(net_copy, numba=False)
                converged = True
            except pp.LoadflowNotConverged:
                converged = False

            loading = (
                net_copy.res_line.loading_percent
                if converged and hasattr(net_copy, "res_line")
                else None
            )
            if not converged or loading is None or loading.isna().any():
                results.append({
                    "line_index": line_idx,
                    "name": name,
                    "n_overloaded": -1,
                    "max_loading_pct": float("inf"),
                    "risk_score": float("inf"),
                    "converged": False,
                    "stage": "ac",
                })
                continue

            risk_score, n_over, max_load = 0.0, 0, 0.0
            for i in loading.index:
                lp = float(loading.at[i])
                if lp > max_load:
                    max_load = lp
                if lp > self.OVERLOAD_THRESHOLD_PCT:
                    n_over += 1
                    risk_score += lp - self.OVERLOAD_THRESHOLD_PCT
            results.append({
                "line_index": line_idx,
                "name": name,
                "n_overloaded": n_over,
                "max_loading_pct": round(max_load, 2),
                "risk_score": round(risk_score, 2),
                "converged": True,
                "stage": "ac",
            })

        results.sort(key=lambda r: r["risk_score"], reverse=True)
        return results

    def run_powerflow(self) -> bool:
        """
        Run AC power flow. Return True if converged, False otherwise.

        Stores convergence state internally so that ``get_grid_state`` can
        detect stale / NaN results from a non-converged run.
        """
        try:
            pp.runpp(self.net, numba=False)
            self._last_pf_converged = True
        except pp.LoadflowNotConverged:
            print("[GridEnvironment] WARNING: Power flow did not converge.")
            self._last_pf_converged = False
            return False
        return True

    def get_grid_state(self) -> GridStateReport:
        """
        Inspect the most recent power-flow results and report overloaded lines.

        Returns:
            GridStateReport with convergence status and overload details.
        """
        import math

        # Guard: non-converged run
        if not self._last_pf_converged:
            return GridStateReport(
                converged=False,
                total_lines=len(self.net.line),
                overloaded_lines=[],
                nominal_lines=0,
                max_loading_percent=0.0,
                summary="Power flow did not converge. Grid state unavailable.",
            )

        # Guard: missing or empty result table
        if not hasattr(self.net, "res_line") or self.net.res_line.empty:
            return GridStateReport(
                converged=False,
                total_lines=len(self.net.line),
                overloaded_lines=[],
                nominal_lines=0,
                max_loading_percent=0.0,
                summary="No power flow results available. Run power flow first.",
            )

        loading = self.net.res_line.loading_percent
        total = len(loading)

        # Guard: NaN values (e.g. from non-converged run polluting res_line)
        if loading.isna().any():
            return GridStateReport(
                converged=False,
                total_lines=total,
                overloaded_lines=[],
                nominal_lines=0,
                max_loading_percent=0.0,
                summary="Power flow results contain NaN values (likely non-converged).",
            )

        overloaded: List[LineState] = []

        for idx in range(total):
            load_pct = float(loading.at[idx])
            if load_pct > self.OVERLOAD_THRESHOLD_PCT:
                overloaded.append(
                    LineState(
                        line_index=idx,
                        name=self.net.line.at[idx, "name"],
                        from_bus=int(self.net.line.at[idx, "from_bus"]),
                        to_bus=int(self.net.line.at[idx, "to_bus"]),
                        loading_percent=round(load_pct, 2),
                        in_service=bool(self.net.line.at[idx, "in_service"]),
                    )
                )

        max_load = float(loading.max()) if total > 0 else 0.0

        # voltage band check (per-net band; distribution feeders run a
        # wider band than transmission)
        vmin = float(self.net.get("vmin_pu", 0.93) or 0.93)
        vmax = float(self.net.get("vmax_pu", 1.07) or 1.07)
        voltage_violations: List[Dict[str, Any]] = []
        for b, row in self.net.res_bus.iterrows():
            vm = float(row.vm_pu)
            if vm == vm and (vm < vmin or vm > vmax):
                voltage_violations.append({
                    "bus": int(b),
                    "name": str(self.net.bus.at[b, "name"]),
                    "vm_pu": round(vm, 4),
                })

        if not overloaded and not voltage_violations:
            summary = (
                f"All {total} lines operating within safe limits. "
                f"Max loading: {max_load:.2f}%."
            )
        else:
            summary = (
                f"{len(overloaded)} of {total} lines overloaded "
                f"(> {self.OVERLOAD_THRESHOLD_PCT}%), "
                f"{len(voltage_violations)} bus voltage violation(s) "
                f"outside [{vmin}, {vmax}] pu. "
                f"Max loading: {max_load:.2f}%."
            )

        wx = self.net.get("weather")
        weather_now = wx.get("now") if wx else None

        return GridStateReport(
            converged=True,
            total_lines=total,
            overloaded_lines=overloaded,
            nominal_lines=total - len(overloaded),
            max_loading_percent=round(max_load, 2),
            summary=summary,
            voltage_violations=voltage_violations,
            weather_now=weather_now,
        )

    def get_action_space(
        self, grid_state: Optional[GridStateReport] = None
    ) -> List[ActionOption]:
        """
        Dynamically generate the valid action space from the current grid.

        Four action types are produced:

        1. **redispatch_up** — increase generator output.
           Margin = max_p_mw - current p_mw.
           Cost sampled from a realistic wholesale range (60-150 EUR/MWh).

        2. **redispatch_down** — decrease generator output.
           Margin = current p_mw - min_p_mw (min_p_mw defaults to 0).
           Cost is low (10-30 EUR/MWh, representing fuel savings).

        3. **curtail_load** — reduce load consumption.
           Margin = current p_mw (down to zero).
           Cost = 10000 EUR/MWh (standard VoLL), or 100000 EUR/MWh for
           critical infrastructure buses.

        4. **discharge_battery** — VPP BESS injects power into the grid.
           Margin = max_p_mw - p_mw (available discharge headroom).
           Cost dynamically undercuts the cheapest generator (Dynamic
           Market Bidding strategy).

        If *grid_state* is provided, each action includes a sensitivity
        hint showing the estimated loading change per MW of action on
        each overloaded line.

        Returns:
            List of ActionOption entries the LLM may select from.
        """
        actions: List[ActionOption] = []

        # Pre-compute sensitivity hints for all buses incident to
        # overloaded lines (typically 2-4 extra PFs total).
        self._sensitivity_cache = self._compute_sensitivities(grid_state) if grid_state else {}

        # Market coupling: with a weather/market forecast attached,
        # redispatch is charged at the current BALANCING price instead of
        # a random wholesale draw (GridGuard parity).
        has_market = bool(self.net.get("weather"))
        bal_price = balancing_price(self.net, 0.0) if has_market else None

        # --- Step A: Generator redispatch UP + collect costs ---------------
        gen_costs: List[float] = []

        for gen_idx in range(len(self.net.gen)):
            gen = self.net.gen.iloc[gen_idx]
            current_p = float(gen["p_mw"])
            max_p = float(gen["max_p_mw"])
            margin = max_p - current_p
            bus = int(gen["bus"])

            if margin <= 0.01:
                continue

            cost = bal_price if has_market else self.random.uniform(
                *self.DEFAULT_REDISPATCH_COST_RANGE)
            gen_costs.append(cost)
            sensitivity = self._compute_sensitivity(
                bus, "redispatch_up", grid_state
            )

            actions.append(
                ActionOption(
                    target_id=gen_idx,
                    action_type="redispatch_up",
                    node_bus=bus,
                    current_p_mw=round(current_p, 2),
                    max_available_mw=round(margin, 2),
                    cost_per_mw=round(cost, 2),
                    sensitivity=sensitivity,
                    description=(
                        f"Increase Gen {gen_idx} (bus {bus}) "
                        f"output by up to {margin:.2f} MW "
                        f"at {cost:.2f} EUR/MWh. [{sensitivity}]"
                    ),
                )
            )

        # --- Step B: Dynamic Market Bidding floor --------------------------
        min_gen_cost = min(gen_costs) if gen_costs else 100.0

        # --- Generator redispatch DOWN actions ------------------------------
        for gen_idx in range(len(self.net.gen)):
            gen = self.net.gen.iloc[gen_idx]
            current_p = float(gen["p_mw"])
            min_p = float(gen.get("min_p_mw", 0.0))
            margin_down = current_p - min_p
            bus = int(gen["bus"])

            if margin_down <= 0.01:
                continue

            cost = (round(0.3 * bal_price, 2) if has_market
                    else self.random.uniform(10.0, 30.0))
            sensitivity = self._compute_sensitivity(
                bus, "redispatch_down", grid_state
            )

            actions.append(
                ActionOption(
                    target_id=gen_idx,
                    action_type="redispatch_down",
                    node_bus=bus,
                    current_p_mw=round(current_p, 2),
                    max_available_mw=round(margin_down, 2),
                    cost_per_mw=round(cost, 2),
                    sensitivity=sensitivity,
                    description=(
                        f"Decrease Gen {gen_idx} (bus {bus}) "
                        f"output by up to {margin_down:.2f} MW "
                        f"at {cost:.2f} EUR/MWh. [{sensitivity}]"
                    ),
                )
            )

        # --- Load curtailment actions --------------------------------------
        for load_idx in range(len(self.net.load)):
            ld = self.net.load.iloc[load_idx]
            current_p = float(ld["p_mw"])
            bus = int(ld["bus"])

            if current_p <= 0.01:
                continue

            is_critical = bus in self.CRITICAL_BUS_IDS
            cost = (
                self.CRITICAL_LOAD_COST
                if is_critical
                else self.DEFAULT_LOAD_CURTAILMENT_COST
            )

            label = "CRITICAL " if is_critical else ""
            sensitivity = self._compute_sensitivity(
                bus, "curtail_load", grid_state
            )

            actions.append(
                ActionOption(
                    target_id=load_idx,
                    action_type="curtail_load",
                    node_bus=bus,
                    current_p_mw=round(current_p, 2),
                    max_available_mw=round(current_p, 2),
                    cost_per_mw=round(cost, 2),
                    sensitivity=sensitivity,
                    description=(
                        f"{label}Curtail Load {load_idx} (bus {bus}) "
                        f"by up to {current_p:.2f} MW "
                        f"at {cost:.2f} EUR/MWh. [{sensitivity}]"
                    ),
                )
            )

        # --- Step C: BESS actions (state-of-charge aware) -------------------
        # Cycling wear cost: 10 EUR/MWh — undercuts both the balancing
        # market and every generator, so BESS is always the cheapest MW
        # (GridGuard parity; replaces the old dynamic bid floor).
        storage_cost = 10.0
        _ = min_gen_cost  # retained for the legacy dynamic-bid log line

        for st_idx in range(len(self.net.storage)):
            st = self.net.storage.iloc[st_idx]
            current_p = float(st["p_mw"])   # pp convention: + = charging
            max_p = float(st["max_p_mw"])
            min_p = float(st["min_p_mw"])
            soc = float(st.get("soc_percent", 100.0) or 100.0) / 100.0
            e_mwh = float(st.get("max_e_mwh", 1e9) or 1e9)
            bus = int(st["bus"])
            target_id = f"storage_{st_idx}"

            # discharge headroom: toward min_p, limited to 1 h of stored
            # energy at the current state of charge
            margin_dis = min(current_p - min_p, max(0.0, soc * e_mwh))
            if margin_dis > 0.01:
                sensitivity = self._compute_sensitivity(
                    bus, "discharge_battery", grid_state
                )
                actions.append(
                    ActionOption(
                        target_id=target_id,
                        action_type="discharge_battery",
                        node_bus=bus,
                        current_p_mw=round(current_p, 2),
                        max_available_mw=round(margin_dis, 2),
                        cost_per_mw=storage_cost,
                        sensitivity=sensitivity,
                        description=(
                            f"Discharge BESS {st_idx} (bus {bus}, "
                            f"SoC {soc*100:.0f}%) by up to {margin_dis:.2f} MW "
                            f"at {storage_cost:.2f} EUR/MWh. [{sensitivity}]"
                        ),
                    )
                )

            # charge headroom: toward max_p, limited by free energy —
            # absorbs excess renewable infeed (reverse-flow overloads)
            margin_chg = min(max_p - current_p, max(0.0, (1 - soc) * e_mwh))
            if margin_chg > 0.01:
                sensitivity = self._compute_sensitivity(
                    bus, "charge_battery", grid_state
                )
                actions.append(
                    ActionOption(
                        target_id=target_id,
                        action_type="charge_battery",
                        node_bus=bus,
                        current_p_mw=round(current_p, 2),
                        max_available_mw=round(margin_chg, 2),
                        cost_per_mw=storage_cost,
                        sensitivity=sensitivity,
                        description=(
                            f"Charge BESS {st_idx} (bus {bus}, "
                            f"SoC {soc*100:.0f}%) by up to {margin_chg:.2f} MW "
                            f"at {storage_cost:.2f} EUR/MWh. [{sensitivity}]"
                        ),
                    )
                )

        # --- Step D: Renewable curtailment (solar / wind spill) ------------
        for sg_idx in range(len(self.net.sgen)):
            sg = self.net.sgen.iloc[sg_idx]
            current_p = float(sg["p_mw"])
            if current_p <= 0.01:
                continue
            bus = int(sg["bus"])
            kind = str(sg.get("type") or "PV")
            label = {"WT": "wind farm", "PV": "utility PV"}.get(
                kind, "rooftop PV")
            sensitivity = self._compute_sensitivity(
                bus, "curtail_renewable", grid_state
            )
            actions.append(
                ActionOption(
                    target_id=f"sgen_{sg_idx}",
                    action_type="curtail_renewable",
                    node_bus=bus,
                    current_p_mw=round(current_p, 2),
                    max_available_mw=round(current_p, 2),
                    cost_per_mw=50.0,
                    sensitivity=sensitivity,
                    description=(
                        f"Curtail {label} {sg_idx} (bus {bus}) by up to "
                        f"{current_p:.2f} MW at 50.00 EUR/MWh spilled. "
                        f"[{sensitivity}]"
                    ),
                )
            )

        return actions

    def _compute_sensitivities(
        self,
        grid_state: GridStateReport,
        delta_mw: Optional[float] = None,
    ) -> Dict[Tuple[int, str], str]:
        """
        Pre-compute directional sensitivity hints via incremental PF.

        Only perturbs buses that are incident to overloaded lines and
        that have controllable resources.  Typically 2-6 extra PFs total.

        Returns:
            Dict mapping (bus, action_type) -> sensitivity string.
        """
        import copy as _copy

        sensitivities: Dict[Tuple[int, str], str] = {}
        if delta_mw is None:
            # probe size scaled to the grid (2 % of load): 0.5 MW on the
            # IEEE 123 feeder, capped at 10 MW on transmission cases
            delta_mw = getattr(self, "_delta_mw", 10.0)

        if not grid_state.overloaded_lines:
            return sensitivities

        # Collect relevant buses: those incident to overloaded lines, PLUS
        # every bus that hosts a controllable resource. On a radial feeder
        # the overloaded lines are the backbone near the substation, while
        # the BESS / PV / wind sit downstream — so probing only the
        # incident buses would hide the fact that discharging a downstream
        # battery relieves the whole upstream corridor. Probing the
        # resource buses too gives the LLM the real per-MW relief numbers.
        relevant: set = set()
        for ol in grid_state.overloaded_lines:
            relevant.add(ol.from_bus)
            relevant.add(ol.to_bus)
        for tbl in (self.net.storage, self.net.sgen, self.net.gen):
            for b in tbl.bus:
                relevant.add(int(b))

        # Snapshot pre-perturbation loading
        pre_loads: Dict[int, float] = {}
        for ol in grid_state.overloaded_lines:
            pre_loads[ol.line_index] = ol.loading_percent

        # Pre-compute for all action types at each relevant bus
        for bus in sorted(relevant):
            for atype in ("redispatch_up", "redispatch_down", "curtail_load",
                          "discharge_battery", "charge_battery",
                          "curtail_renewable"):
                key = (bus, atype)
                # Check if any action actually targets this bus
                has_resource = False
                if atype in ("redispatch_up", "redispatch_down"):
                    for g_idx in range(len(self.net.gen)):
                        if int(self.net.gen.at[g_idx, "bus"]) == bus:
                            current = float(self.net.gen.at[g_idx, "p_mw"])
                            if atype == "redispatch_up":
                                max_p = float(self.net.gen.at[g_idx, "max_p_mw"])
                                if max_p - current > 0.01:
                                    has_resource = True
                            else:  # redispatch_down
                                min_p = float(self.net.gen.iloc[g_idx].get("min_p_mw", 0.0) or 0.0)
                                if current - min_p > 0.01:
                                    has_resource = True
                            if has_resource:
                                break
                elif atype == "discharge_battery":
                    for st_idx in range(len(self.net.storage)):
                        if int(self.net.storage.at[st_idx, "bus"]) == bus:
                            current = float(self.net.storage.at[st_idx, "p_mw"])
                            min_p = float(self.net.storage.at[st_idx, "min_p_mw"])
                            if current - min_p > 0.01:
                                has_resource = True
                                break
                elif atype == "charge_battery":
                    for st_idx in range(len(self.net.storage)):
                        if int(self.net.storage.at[st_idx, "bus"]) == bus:
                            current = float(self.net.storage.at[st_idx, "p_mw"])
                            max_p = float(self.net.storage.at[st_idx, "max_p_mw"])
                            if max_p - current > 0.01:
                                has_resource = True
                                break
                elif atype == "curtail_renewable":
                    for sg_idx in range(len(self.net.sgen)):
                        if int(self.net.sgen.at[sg_idx, "bus"]) == bus:
                            if float(self.net.sgen.at[sg_idx, "p_mw"]) > 0.01:
                                has_resource = True
                                break
                else:
                    for l_idx in range(len(self.net.load)):
                        if int(self.net.load.at[l_idx, "bus"]) == bus:
                            if float(self.net.load.at[l_idx, "p_mw"]) > 0.01:
                                has_resource = True
                                break

                if not has_resource:
                    sensitivities[key] = "no controllable resource"
                    continue

                # Apply perturbation
                net_copy = _copy.deepcopy(self.net)

                applied = False
                if atype in ("redispatch_up", "redispatch_down"):
                    # Perturb generator output
                    sign = 1.0 if atype == "redispatch_up" else -1.0
                    amount = delta_mw * sign  # +delta for up, -delta for down
                    for g_idx in range(len(net_copy.gen)):
                        if int(net_copy.gen.at[g_idx, "bus"]) == bus:
                            current = float(net_copy.gen.at[g_idx, "p_mw"])
                            max_p = float(net_copy.gen.at[g_idx, "max_p_mw"])
                            min_p = float(net_copy.gen.iloc[g_idx].get("min_p_mw", 0.0) or 0.0)
                            net_copy.gen.at[g_idx, "p_mw"] = max(min_p, min(max_p, current + amount))
                            applied = True
                            break
                elif atype == "discharge_battery":
                    # Discharge battery: p_mw goes more negative (inject power)
                    for st_idx in range(len(net_copy.storage)):
                        if int(net_copy.storage.at[st_idx, "bus"]) == bus:
                            current = float(net_copy.storage.at[st_idx, "p_mw"])
                            max_p = float(net_copy.storage.at[st_idx, "max_p_mw"])
                            min_p = float(net_copy.storage.at[st_idx, "min_p_mw"])
                            # Discharging reduces p_mw (more negative = more power out)
                            new_p = max(min_p, current - delta_mw)
                            net_copy.storage.at[st_idx, "p_mw"] = new_p
                            applied = True
                            break
                elif atype == "charge_battery":
                    # Charge battery: p_mw goes more positive (consume power)
                    for st_idx in range(len(net_copy.storage)):
                        if int(net_copy.storage.at[st_idx, "bus"]) == bus:
                            current = float(net_copy.storage.at[st_idx, "p_mw"])
                            max_p = float(net_copy.storage.at[st_idx, "max_p_mw"])
                            net_copy.storage.at[st_idx, "p_mw"] = min(
                                max_p, current + delta_mw)
                            applied = True
                            break
                elif atype == "curtail_renewable":
                    # Spill renewable output: sgen p_mw down
                    for sg_idx in range(len(net_copy.sgen)):
                        if int(net_copy.sgen.at[sg_idx, "bus"]) == bus:
                            current = float(net_copy.sgen.at[sg_idx, "p_mw"])
                            net_copy.sgen.at[sg_idx, "p_mw"] = max(
                                0.0, current - delta_mw)
                            applied = True
                            break
                else:
                    # curtail_load: reduce load (equivalent to +delta_mw injection)
                    for l_idx in range(len(net_copy.load)):
                        if int(net_copy.load.at[l_idx, "bus"]) == bus:
                            current = float(net_copy.load.at[l_idx, "p_mw"])
                            net_copy.load.at[l_idx, "p_mw"] = max(0.0, current - delta_mw)
                            applied = True
                            break

                if not applied:
                    sensitivities[key] = "no controllable resource"
                    continue

                # Re-run
                try:
                    pp.runpp(net_copy, numba=False)
                except pp.LoadflowNotConverged:
                    sensitivities[key] = "non-convergent perturbation"
                    continue

                if not hasattr(net_copy, "res_line") or net_copy.res_line.empty:
                    sensitivities[key] = "no results"
                    continue

                post_loading = net_copy.res_line.loading_percent

                parts: List[str] = []
                for ol_idx, pre_pct in pre_loads.items():
                    if post_loading.isna().at[ol_idx]:
                        continue
                    post_pct = float(post_loading.at[ol_idx])
                    delta_pct = post_pct - pre_pct
                    delta_per_mw = delta_pct / delta_mw
                    direction = "relief" if delta_pct < 0 else "increase"
                    parts.append(
                        f"{delta_per_mw:+.3f} pp {direction} on L{ol_idx} per MW"
                    )

                sensitivities[key] = "; ".join(parts) if parts else "no measurable impact"

        return sensitivities

    def _compute_sensitivity(
        self,
        bus: int,
        action_type: str,
        grid_state: Optional[GridStateReport],
    ) -> str:
        """
        Look up a pre-computed sensitivity hint.

        Falls back to 'indirect relief only' for buses not incident
        to any overloaded line.
        """
        if grid_state is None or not grid_state.overloaded_lines:
            return "no overload data"

        if not hasattr(self, "_sensitivity_cache"):
            self._sensitivity_cache = self._compute_sensitivities(grid_state)

        key = (bus, action_type)
        cached = self._sensitivity_cache.get(key)
        if cached is not None:
            return cached
        return "indirect relief only"

    def execute_actions(self, actions: List[Dict[str, Any]]) -> bool:
        """
        Apply a list of LLM-issued commands to the grid.

        Each action dict must contain:
            - target_id   (int or str): index into net.gen, net.load, or
                          'storage_N' for BESS assets
            - action_type (str): 'redispatch_up', 'redispatch_down',
                                 'curtail_load', 'discharge_battery'
            - amount_mw   (float): MW to change

        Returns:
            True if all actions were applied, False if any validation failed.
        """
        for i, action in enumerate(actions):
            target_id = action.get("target_id")
            action_type = action.get("action_type")
            amount_mw = action.get("amount_mw")

            if target_id is None or action_type is None or amount_mw is None:
                print(
                    f"[GridEnvironment] Action {i} is malformed: {action}. "
                    f"Skipping."
                )
                return False

            amount_mw = float(amount_mw)

            # Storage target_ids are strings like "storage_0"
            if action_type == "discharge_battery":
                if not isinstance(target_id, str) or not target_id.startswith("storage_"):
                    print(
                        f"[GridEnvironment] Action {i}: invalid target_id "
                        f"'{target_id}' for discharge_battery.  "
                        f"Expected 'storage_<N>'.  Skipping."
                    )
                    return False
                st_idx = int(target_id.split("_", 1)[1])
                if st_idx < 0 or st_idx >= len(self.net.storage):
                    print(
                        f"[GridEnvironment] Invalid storage index {st_idx}."
                    )
                    return False

                st = self.net.storage.iloc[st_idx]
                current = float(st["p_mw"])
                min_allowed = float(st["min_p_mw"])
                soc = float(st.get("soc_percent", 100.0) or 100.0) / 100.0
                e_mwh = float(st.get("max_e_mwh", 1e9) or 1e9)

                # state-of-charge limit: cannot sustain more than 1 h of
                # stored energy (GridGuard parity)
                soc_limit = max(0.0, soc * e_mwh)
                grid_out = -current  # positive = already injecting
                if grid_out + amount_mw > soc_limit + 0.01:
                    print(
                        f"[GridEnvironment] BESS {st_idx}: SoC {soc*100:.0f}% "
                        f"holds {soc_limit:.2f} MWh — clamping discharge."
                    )
                    amount_mw = max(0.0, soc_limit - grid_out)

                # Discharging: p_mw goes more negative (or from 0 to negative)
                new_p = current - amount_mw
                if new_p < min_allowed - 0.01:
                    print(
                        f"[GridEnvironment] Storage {st_idx} (bus {int(st['bus'])}): "
                        f"requested discharge {amount_mw:.2f} MW exceeds "
                        f"available headroom (limit {min_allowed:.2f} MW). Clamping."
                    )
                    amount_mw = current - min_allowed
                    new_p = min_allowed

                self.net.storage.at[st_idx, "p_mw"] = new_p
                print(
                    f"[GridEnvironment] BESS {st_idx} (bus {int(st['bus'])}): "
                    f"{current:.2f} -> {new_p:.2f} MW "
                    f"(discharged {current - new_p:.2f} MW to grid, "
                    f"SoC {soc*100:.0f}%)."
                )
                continue

            if action_type == "charge_battery":
                if not isinstance(target_id, str) or not target_id.startswith("storage_"):
                    print(
                        f"[GridEnvironment] Action {i}: invalid target_id "
                        f"'{target_id}' for charge_battery. Skipping."
                    )
                    return False
                st_idx = int(target_id.split("_", 1)[1])
                if st_idx < 0 or st_idx >= len(self.net.storage):
                    print(f"[GridEnvironment] Invalid storage index {st_idx}.")
                    return False
                st = self.net.storage.iloc[st_idx]
                current = float(st["p_mw"])
                max_p = float(st["max_p_mw"])
                soc = float(st.get("soc_percent", 100.0) or 100.0) / 100.0
                e_mwh = float(st.get("max_e_mwh", 1e9) or 1e9)
                free = max(0.0, (1 - soc) * e_mwh)
                amount_mw = min(amount_mw, free, max_p - current)
                new_p = current + amount_mw
                self.net.storage.at[st_idx, "p_mw"] = new_p
                print(
                    f"[GridEnvironment] BESS {st_idx} (bus {int(st['bus'])}): "
                    f"{current:.2f} -> {new_p:.2f} MW "
                    f"(charging {amount_mw:.2f} MW from grid, "
                    f"SoC {soc*100:.0f}%)."
                )
                continue

            if action_type == "curtail_renewable":
                if not isinstance(target_id, str) or not target_id.startswith("sgen_"):
                    print(
                        f"[GridEnvironment] Action {i}: invalid target_id "
                        f"'{target_id}' for curtail_renewable. "
                        f"Expected 'sgen_<N>'. Skipping."
                    )
                    return False
                sg_idx = int(target_id.split("_", 1)[1])
                if sg_idx < 0 or sg_idx >= len(self.net.sgen):
                    print(f"[GridEnvironment] Invalid sgen index {sg_idx}.")
                    return False
                sg = self.net.sgen.iloc[sg_idx]
                current = float(sg["p_mw"])
                amount_mw = min(amount_mw, current)
                self.net.sgen.at[sg_idx, "p_mw"] = current - amount_mw
                print(
                    f"[GridEnvironment] Renewable {sg_idx} "
                    f"('{sg['name']}', bus {int(sg['bus'])}): "
                    f"{current:.2f} -> {current - amount_mw:.2f} MW "
                    f"(spilled {amount_mw:.2f} MW)."
                )
                continue

            target_id = int(target_id)

            if action_type == "redispatch_up":
                if target_id < 0 or target_id >= len(self.net.gen):
                    print(
                        f"[GridEnvironment] Invalid gen index {target_id}."
                    )
                    return False

                gen = self.net.gen.iloc[target_id]
                current = float(gen["p_mw"])
                max_p = float(gen["max_p_mw"])
                new_p = current + amount_mw

                if new_p > max_p + 0.01:
                    print(
                        f"[GridEnvironment] Gen {target_id}: "
                        f"requested {amount_mw:.2f} MW increase exceeds "
                        f"max capacity ({max_p:.2f} MW). Clamping."
                    )
                    amount_mw = max_p - current
                    new_p = max_p

                self.net.gen.at[target_id, "p_mw"] = new_p
                print(
                    f"[GridEnvironment] Gen {target_id}: "
                    f"{current:.2f} -> {new_p:.2f} MW "
                    f"(+{new_p - current:.2f} MW)."
                )

            elif action_type == "redispatch_down":
                if target_id < 0 or target_id >= len(self.net.gen):
                    print(
                        f"[GridEnvironment] Invalid gen index {target_id}."
                    )
                    return False

                gen = self.net.gen.iloc[target_id]
                current = float(gen["p_mw"])
                min_p = float(gen.get("min_p_mw", 0.0))
                new_p = current - amount_mw

                if new_p < min_p - 0.01:
                    print(
                        f"[GridEnvironment] Gen {target_id}: "
                        f"requested {amount_mw:.2f} MW decrease exceeds "
                        f"min capacity ({min_p:.2f} MW). Clamping."
                    )
                    amount_mw = current - min_p
                    new_p = min_p

                self.net.gen.at[target_id, "p_mw"] = new_p
                print(
                    f"[GridEnvironment] Gen {target_id}: "
                    f"{current:.2f} -> {new_p:.2f} MW "
                    f"(-{current - new_p:.2f} MW)."
                )

            elif action_type == "curtail_load":
                if target_id < 0 or target_id >= len(self.net.load):
                    print(
                        f"[GridEnvironment] Invalid load index {target_id}."
                    )
                    return False

                ld = self.net.load.iloc[target_id]
                current = float(ld["p_mw"])
                if amount_mw > current + 0.01:
                    print(
                        f"[GridEnvironment] Load {target_id}: "
                        f"requested curtailment {amount_mw:.2f} MW exceeds "
                        f"current load ({current:.2f} MW). Clamping."
                    )
                    amount_mw = current

                new_p = current - amount_mw
                self.net.load.at[target_id, "p_mw"] = max(new_p, 0.0)
                print(
                    f"[GridEnvironment] Load {target_id}: "
                    f"{current:.2f} -> {max(new_p, 0.0):.2f} MW "
                    f"(-{amount_mw:.2f} MW)."
                )

            else:
                print(
                    f"[GridEnvironment] Unknown action_type "
                    f"'{action_type}' at index {i}. Skipping."
                )
                return False

        # Re-run power flow after all actions are applied
        self.run_powerflow()
        return True

    # ------------------------------------------------------------------
    # Operational data feeds (ported from GridGuard)
    # ------------------------------------------------------------------

    def get_scada_measurements(self) -> Dict[str, Any]:
        """Real-time SCADA telemetry: MW/MVAR/kA flow per in-service line,
        bus voltage magnitude/angle, and the substation exchange."""
        net = self.net
        lines = []
        for i in net.line.index:
            if not bool(net.line.at[i, "in_service"]):
                continue
            r = net.res_line.loc[i]
            lines.append({
                "line": int(i),
                "name": str(net.line.at[i, "name"]),
                "from_bus": int(net.line.at[i, "from_bus"]),
                "to_bus": int(net.line.at[i, "to_bus"]),
                "p_from_mw": round(float(r.p_from_mw), 3),
                "q_from_mvar": round(float(r.q_from_mvar), 3),
                "i_ka": round(float(r.i_from_ka), 4),
                "loading_percent": round(float(r.loading_percent), 1),
            })
        buses = [
            {"bus": int(i), "name": str(net.bus.at[i, "name"]),
             "vm_pu": round(float(r.vm_pu), 4),
             "va_degree": round(float(r.va_degree), 2)}
            for i, r in net.res_bus.iterrows() if r.vm_pu == r.vm_pu
        ]
        return {
            "line_flows": lines,
            "bus_voltages": buses,
            "substation_p_mw": round(float(net.res_ext_grid.p_mw.sum()), 3),
            "substation_q_mvar": round(float(net.res_ext_grid.q_mvar.sum()), 3),
        }

    def get_asset_health(self) -> Dict[str, Any]:
        """Condition register: health indices, thermal deratings already
        baked into the ratings, and lines out for scheduled maintenance."""
        reg = self.net.get("asset_health")
        if not reg:
            return {"error": "no asset-health register on this scenario"}
        degraded = [
            {"line": i, "name": str(self.net.line.at[i, "name"]), **v}
            for i, v in sorted(reg["lines"].items())
            if v["derate"] < 1.0
        ]
        return {
            "degraded_lines": degraded,
            "maintenance_lines": [
                {"line": i, "name": str(self.net.line.at[i, "name"])}
                for i in reg.get("maintenance_lines", [])
            ],
            "maintenance_scheduled": [
                {"line": i, "name": str(self.net.line.at[i, "name"])}
                for i in reg.get("maintenance_scheduled", [])
            ],
            "healthy_lines": len(reg["lines"]) - len(degraded),
        }

    def get_weather_forecast(self) -> Dict[str, Any]:
        """12-hour weather + renewable + load + market forecast."""
        wx = self.net.get("weather")
        if not wx:
            return {"error": "scenario has no weather forecast"}
        return {
            "profile": wx["profile"],
            "current_hour": wx["hour"],
            "forecast": wx["forecast"],
        }

    def advance_hour(self) -> Dict[str, Any]:
        """Step one hour along the forecast: batteries integrate state of
        charge, renewables and temperature-driven demand follow the sky."""
        wx = self.net.get("weather")
        if not wx:
            return {"ok": False, "error": "scenario has no weather forecast"}
        elapsed = (wx["hour"] - wx["forecast"][0]["hour"]) % 24
        nxt = elapsed + 1
        if nxt >= len(wx["forecast"]):
            return {"ok": False, "error": "end of forecast horizon"}

        for s in self.net.storage.index:
            e = float(self.net.storage.at[s, "max_e_mwh"])
            p = float(self.net.storage.at[s, "p_mw"])  # + = charging
            soc = float(self.net.storage.at[s, "soc_percent"]) + p / e * 100.0
            if soc <= 0.5 or soc >= 99.5:
                self.net.storage.at[s, "p_mw"] = 0.0
            self.net.storage.at[s, "soc_percent"] = min(max(soc, 0.0), 100.0)

        entry = wx["forecast"][nxt]
        apply_weather(self.net, entry)
        wx["hour"] = entry["hour"]
        ok = self.run_powerflow()
        return {"ok": ok, "weather": entry}

    def context_block(self) -> str:
        """Compact operational context for the LLM prompt: weather now,
        the forecast ahead, market prices, BESS state and asset health."""
        out: List[str] = []
        wx = self.net.get("weather")
        if wx and wx.get("now"):
            n = wx["now"]
            out += [
                "## Weather, Market & Forecast",
                (f"Now {n['hour']:02d}:00 — {n['condition']}, {n['temp_c']}°C, "
                 f"wind {n['wind_ms']} m/s, irradiance {n['irradiance_wm2']} W/m², "
                 f"demand x{n['load_factor']}. Day-ahead "
                 f"{n['day_ahead_price']} EUR/MWh, BALANCING "
                 f"{n['balancing_price']} EUR/MWh (redispatch is charged at "
                 f"the balancing price)."),
                "Next hours (hour / cond / wind m/s / solar MW / wind MW / "
                "load MW / bal EUR):",
            ]
            cur = wx["hour"]
            start = (cur - wx["forecast"][0]["hour"]) % 24
            for e in wx["forecast"][start:start + 6]:
                cut = " CUT-OUT" if (e["wind_factor"] == 0 and e["wind_ms"] >= 25) else ""
                out.append(
                    f"  {e['hour']:02d}:00 {e['condition']:<9} "
                    f"{e['wind_ms']:>4} {e.get('solar_forecast_mw', 0):>6} "
                    f"{e.get('wind_forecast_mw', 0):>6}{cut} "
                    f"{e.get('load_forecast_mw', 0):>7} "
                    f"{e['balancing_price']:>6}"
                )
            out.append(
                "Wind farms CUT OUT above 25 m/s. Do not lean a fix on "
                "renewable output the forecast is about to remove; keep "
                "battery energy in reserve if demand or prices are rising."
            )
        if len(self.net.storage):
            out.append("## BESS state")
            for i, st in self.net.storage.iterrows():
                out.append(
                    f"  storage_{i}: bus {int(st.bus)}, "
                    f"SoC {float(st.soc_percent):.0f}%, "
                    f"setpoint {-float(st.p_mw):.2f} MW to grid, "
                    f"rating ±{float(st.max_p_mw):.2f} MW, "
                    f"{float(st.max_e_mwh):.2f} MWh"
                )
        ah = self.get_asset_health()
        if "error" not in ah:
            out.append("## Asset health")
            for d in ah["degraded_lines"]:
                out.append(
                    f"  line {d['line']} ('{d['name']}'): health "
                    f"{d['health']*100:.0f}%, thermal rating derated x{d['derate']} "
                    f"(already reflected in loading_percent)"
                )
            if ah["maintenance_lines"]:
                names = ", ".join(
                    f"{m['line']} ('{m['name']}')" for m in ah["maintenance_lines"])
                out.append(
                    f"  maintenance outage (cannot be reclosed): line {names}")
            if ah.get("maintenance_scheduled"):
                names = ", ".join(
                    f"{m['line']} ('{m['name']}')" for m in ah["maintenance_scheduled"])
                out.append(
                    f"  scheduled for maintenance (treat as no spare capacity): "
                    f"line {names}")
        return "\n".join(out)
