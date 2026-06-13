"""
scenario.py — DER, weather and asset-health scenario pipeline.

Ported from GridGuard and re-sized for distribution feeders: renewable
and storage capacities are fractions of total feeder load rather than
fixed MW, so the same code dresses the 3.5 MW IEEE 123 feeder or a
200+ MW transmission case with a sensible DER fleet.

Adds to any pandapower net:
  - residential rooftop PV on the largest-load buses (sgen type PV_res)
  - one utility-scale PV plant and two wind farms on light buses
    (types PV / WT; wind follows a turbine power curve with 25 m/s
    cut-out via weather.apply_weather)
  - battery energy storage (BESS) co-located with the biggest loads
    (pandapower sign convention: storage p_mw > 0 = charging)
  - a 12-hour weather + market forecast (net["weather"]) driving solar,
    wind and temperature-dependent demand
  - an asset-health register (net["asset_health"]): two lines thermally
    derated, one low-impact line out for scheduled maintenance
"""

from __future__ import annotations

import random
from typing import Optional

import pandapower as pp

from weather import apply_weather, make_forecast

# DER sizing as fractions of total feeder/grid load
ROOFTOP_FRACS = (0.07, 0.06, 0.05, 0.05, 0.04, 0.04)   # 6 clusters, ~31 %
UTILITY_PV_FRAC = 0.14
WIND_FRACS = (0.14, 0.09)
BESS_P_FRAC = 0.11          # power rating per battery
BESS_HOURS = 2.5            # energy = power x hours
BESS_SOC0 = 60.0
N_BESS = 3


def add_der(net: pp.pandapowerNet) -> None:
    """Attach rooftop PV, utility PV, wind and BESS, sized to the load."""
    total = float(net.load.p_mw.sum())
    by_load = net.load.sort_values("p_mw", ascending=False)
    big = [int(b) for b in by_load.bus.head(len(ROOFTOP_FRACS))]

    for bus, frac in zip(big, ROOFTOP_FRACS):
        pp.create_sgen(net, bus, p_mw=0.0, q_mvar=0.0,
                       name=f"rooftop@{net.bus.at[bus, 'name']}",
                       max_p_mw=round(total * frac, 3), min_p_mw=0.0,
                       type="PV_res")

    taken = set(big) | set(int(b) for b in net.ext_grid.bus)
    if len(net.gen):
        taken |= set(int(b) for b in net.gen.bus)
    load_at = {int(l.bus): float(l.p_mw) for _, l in net.load.iterrows()}
    rural = sorted((int(b) for b in net.bus.index if int(b) not in taken),
                   key=lambda b: load_at.get(b, 0.0))
    sites = rural[: len(WIND_FRACS) + 1]
    for bus, frac in zip(sites[: len(WIND_FRACS)], WIND_FRACS):
        pp.create_sgen(net, bus, p_mw=0.0, q_mvar=0.0,
                       name=f"windfarm@{net.bus.at[bus, 'name']}",
                       max_p_mw=round(total * frac, 3), min_p_mw=0.0,
                       type="WT")
    if len(sites) > len(WIND_FRACS):
        bus = sites[len(WIND_FRACS)]
        pp.create_sgen(net, bus, p_mw=0.0, q_mvar=0.0,
                       name=f"utilityPV@{net.bus.at[bus, 'name']}",
                       max_p_mw=round(total * UTILITY_PV_FRAC, 3),
                       min_p_mw=0.0, type="PV")

    p_bess = round(total * BESS_P_FRAC, 3)
    for bus in big[:N_BESS]:
        pp.create_storage(net, bus, p_mw=0.0,
                          max_e_mwh=round(p_bess * BESS_HOURS, 3),
                          soc_percent=BESS_SOC0,
                          min_p_mw=-p_bess, max_p_mw=p_bess,
                          name=f"BESS@{net.bus.at[bus, 'name']}")


def add_asset_health(net: pp.pandapowerNet, seed: int = 7) -> None:
    """Derate two lines, take one low-impact line out for maintenance."""
    rng = random.Random(f"assets|{seed}")
    register: dict = {"lines": {}, "trafos": {}, "maintenance_lines": []}

    lines = [int(i) for i in net.line.index]
    weak = rng.sample(lines, k=min(2, len(lines)))
    for idx in lines:
        if idx in weak:
            health = round(rng.uniform(0.35, 0.55), 2)
        else:
            health = round(rng.uniform(0.82, 1.0), 2)
        derate = 1.0 if health >= 0.8 else round(0.75 + 0.25 * health, 3)
        register["lines"][idx] = {"health": health, "derate": derate}
        if derate < 1.0:
            net.line.at[idx, "max_i_ka"] *= derate

    # Maintenance outage. On a MESHED transmission grid we take the
    # lowest-loaded redundant line out for real (it cannot be reclosed).
    # A single-ring distribution feeder has only one redundant path, so
    # physically removing a ring segment would de-mesh it (every later
    # N-1 would then island) — there we keep the line in service but flag
    # the least-loaded non-islanding ring segment as SCHEDULED for
    # maintenance (informational: the agent must not rely on it as spare
    # capacity, but the ring stays closed).
    import pandapower.topology as top

    is_feeder = bool(net.get("tie_lines"))
    register["maintenance_scheduled"] = []
    try:
        pp.runpp(net, numba=False)
        cands = sorted(
            (float(net.res_line.at[i, "loading_percent"]), int(i))
            for i in net.line.index
            if bool(net.line.at[i, "in_service"])
        )
        for _, mline in cands:
            net.line.at[mline, "in_service"] = False
            islands = bool(top.unsupplied_buses(net))
            if not islands and not is_feeder:
                register["maintenance_lines"].append(mline)  # real outage
                break
            net.line.at[mline, "in_service"] = True          # revert
            if not islands and is_feeder:
                register["maintenance_scheduled"].append(mline)
                break
    except pp.LoadflowNotConverged:
        pass

    net["asset_health"] = register


def prepare_scenario(
    net: pp.pandapowerNet,
    load_scale: float = 1.0,
    weather: Optional[str] = "clear",
    weather_hour: int = 14,
    seed: int = 7,
    with_der: bool = True,
    with_asset_health: bool = True,
) -> pp.pandapowerNet:
    """Apply the full scenario pipeline to a pandapower net in place."""
    net.load["p_mw"] *= load_scale
    net.load["q_mvar"] *= load_scale

    if with_der:
        add_der(net)

    if weather:
        net["weather"] = {
            "profile": weather,
            "hour": weather_hour,
            "forecast": make_forecast(weather, start_hour=weather_hour,
                                      seed=seed),
        }
        net["wx_load_factor"] = 1.0
        apply_weather(net, net["weather"]["forecast"][0])

        # forecasts in MW terms now that DER capacities exist
        if len(net.sgen):
            pv_cap = float(net.sgen.loc[net.sgen.type != "WT", "max_p_mw"].sum())
            wt_cap = float(net.sgen.loc[net.sgen.type == "WT", "max_p_mw"].sum())
        else:
            pv_cap = wt_cap = 0.0
        load_ref = float(net.load.p_mw.sum()) / net["wx_load_factor"]
        for e in net["weather"]["forecast"]:
            e["load_forecast_mw"] = round(load_ref * e["load_factor"], 3)
            e["solar_forecast_mw"] = round(pv_cap * e["solar_factor"], 3)
            e["wind_forecast_mw"] = round(wt_cap * e["wind_factor"], 3)

    if with_asset_health:
        add_asset_health(net, seed=seed)

    return net
