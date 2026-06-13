"""Weather forecasting for GridGuard.

Weather drives both sides of the power balance:
  supply  — solar (rooftop + utility PV) follows irradiance: a clear-sky
            diurnal bell curve attenuated by cloud cover; wind farms
            follow wind speed through a turbine power curve, including
            high-wind CUT-OUT above 25 m/s (a storm first maxes wind
            output, then removes it entirely);
  demand  — load follows temperature: cooling load above 22 °C, heating
            load below 12 °C.

make_forecast() produces a deterministic (seeded) hourly forecast for one
of four profiles, with day-ahead/balancing market prices attached;
apply_weather() pushes a forecast entry into a pandapower net (rescales
renewable sgens and loads). The LLM agent sees the forecast via its tools,
so it can reason about what is coming ("storm front in 3 h kills solar and
cuts out the wind farms — preposition the batteries").
"""

from __future__ import annotations

import math
import random

from market import attach_prices

PROFILES = ("clear", "heatwave", "overcast", "storm")

# attenuation: full overcast removes 80 % of solar output
CLOUD_SOLAR_LOSS = 0.8
# demand response to temperature (per °C outside the comfort band)
COOLING_PER_C = 0.015   # +1.5 % load per °C above 22 °C  (A/C)
HEATING_PER_C = 0.012   # +1.2 % load per °C below 12 °C  (heating)
# turbine power curve (IEC-class onshore turbine)
WIND_CUT_IN_MS = 3.0
WIND_RATED_MS = 12.0
WIND_CUT_OUT_MS = 25.0
# solar irradiance at clear-sky noon
IRRADIANCE_PEAK_WM2 = 1000.0


def clear_sky_factor(hour: int) -> float:
    """Irradiance bell curve: 0 before 06:00 / after 18:00, peak at noon."""
    h = hour % 24
    if h <= 6 or h >= 18:
        return 0.0
    return math.sin(math.pi * (h - 6) / 12.0)


def solar_factor(hour: int, cloud: float) -> float:
    return clear_sky_factor(hour) * (1.0 - CLOUD_SOLAR_LOSS * cloud)


def wind_factor(wind_ms: float) -> float:
    """Turbine power curve: cut-in 3 m/s, rated 12 m/s, CUT-OUT at 25 m/s."""
    if wind_ms < WIND_CUT_IN_MS or wind_ms >= WIND_CUT_OUT_MS:
        return 0.0
    if wind_ms >= WIND_RATED_MS:
        return 1.0
    x = (wind_ms - WIND_CUT_IN_MS) / (WIND_RATED_MS - WIND_CUT_IN_MS)
    return x ** 3


def load_factor(temp_c: float) -> float:
    f = 1.0
    if temp_c > 22.0:
        f += COOLING_PER_C * (temp_c - 22.0)
    if temp_c < 12.0:
        f += HEATING_PER_C * (12.0 - temp_c)
    return round(f, 4)


def _diurnal(hour: int) -> float:
    """-1..1 temperature swing, coolest ~04:00, warmest ~15:00."""
    return math.sin(math.pi * ((hour % 24) - 9) / 12.0)


def make_forecast(
    profile: str = "clear",
    start_hour: int = 14,
    hours: int = 12,
    seed: int = 7,
) -> list[dict]:
    """Hourly forecast entries: hour, condition, temp_c, cloud, and the
    derived solar_factor / load_factor the grid model consumes."""
    if profile not in PROFILES:
        raise ValueError(f"unknown weather profile {profile!r}; pick from {PROFILES}")
    # string seed: deterministic across processes (tuple seeds fall back to
    # per-process randomized hash())
    rng = random.Random(f"{seed}|{profile}|{start_hour}")
    out = []
    for k in range(hours):
        h = start_hour + k
        if profile == "clear":
            base, amp, cloud, cond = 26.0, 5.0, rng.uniform(0.05, 0.18), "clear"
            wind = rng.uniform(5.0, 8.0)
        elif profile == "heatwave":
            base, amp, cloud, cond = 35.0, 5.0, rng.uniform(0.0, 0.08), "heatwave"
            wind = rng.uniform(2.5, 5.0)          # heatwaves are still air
        elif profile == "overcast":
            base, amp, cloud, cond = 19.0, 3.0, rng.uniform(0.7, 0.92), "overcast"
            wind = rng.uniform(8.0, 12.0)
        else:  # storm: front arrives 3 h in, partial clearing after 8 h
            if k < 3:
                base, amp, cloud, cond = 24.0, 4.0, rng.uniform(0.3, 0.45), "pre-storm"
                wind = rng.uniform(10.0, 14.0)
            elif k < 8:
                base, amp, cloud, cond = 17.0, 2.0, rng.uniform(0.9, 1.0), "storm"
                wind = rng.uniform(20.0, 28.0)    # gusts beyond 25 m/s cut out
            else:
                base, amp, cloud, cond = 19.0, 3.0, rng.uniform(0.45, 0.6), "clearing"
                wind = rng.uniform(9.0, 13.0)
        temp = round(base + amp * _diurnal(h) + rng.uniform(-0.6, 0.6), 1)
        sf = solar_factor(h, cloud)
        out.append(
            {
                "hour": h % 24,
                "condition": cond,
                "temp_c": temp,
                "cloud": round(cloud, 2),
                "wind_ms": round(wind, 1),
                "irradiance_wm2": round(IRRADIANCE_PEAK_WM2 * sf),
                "solar_factor": round(sf, 3),
                "wind_factor": round(wind_factor(wind), 3),
                "load_factor": load_factor(temp),
            }
        )
    return attach_prices(out)


def apply_weather(net, entry: dict) -> None:
    """Push one forecast entry into the net: set solar sgen output from
    capacity x solar_factor and rescale loads to the new load_factor.

    Loads are scaled by the RATIO of new to previously applied factor, so
    operator curtailments and the scenario's load_scale are preserved.
    Renewables are recomputed from capacity (PV types follow solar_factor,
    wind farms the turbine power curve), so per-hour curtailment resets as
    the weather itself changes the resource.
    """
    prev = float(net.get("wx_load_factor", 1.0))
    new = float(entry["load_factor"])
    ratio = new / prev if prev else new
    net.load["p_mw"] *= ratio
    net.load["q_mvar"] *= ratio
    net["wx_load_factor"] = new

    for i in net.sgen.index:
        kind = str(net.sgen.at[i, "type"] or "PV")
        factor = entry["wind_factor"] if kind == "WT" else entry["solar_factor"]
        net.sgen.at[i, "p_mw"] = float(net.sgen.at[i, "max_p_mw"]) * float(factor)

    wx = net.get("weather")
    if wx is not None:
        wx["now"] = entry
