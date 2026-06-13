"""Electricity market prices for GridGuard.

Two prices per hour, both $/MWh, derived from the same fundamentals the
forecast carries (temperature-driven demand, wind and solar infeed):

  day-ahead   what energy cleared at in yesterday's auction — the
              reference cost of scheduled generation;
  balancing   what the TSO pays right now for corrective energy
              (redispatch up, battery discharge displacing energy).
              Always at a premium over day-ahead.

GridGuard charges redispatch at the *balancing* price of the current
hour, so the cost of fixing the grid moves with system stress: scarcity
hours (heatwave evening, storm wind cut-out) make corrective actions
expensive, renewable-rich hours make them cheap.
"""

from __future__ import annotations

DA_BASE = 35.0          # $/MWh in a calm, average hour
DA_DEMAND_SLOPE = 80.0  # scarcity: $ per unit of load_factor above 0.95
DA_SOLAR_DEPTH = 12.0   # merit-order effect of solar infeed
DA_WIND_DEPTH = 8.0     # merit-order effect of wind infeed
DA_FLOOR = 5.0
BAL_MARKUP = 1.45       # balancing premium over day-ahead
BAL_ADDER = 12.0


def attach_prices(forecast: list[dict]) -> list[dict]:
    """Add day_ahead_price / balancing_price to each forecast entry."""
    for e in forecast:
        da = (
            DA_BASE
            + DA_DEMAND_SLOPE * max(0.0, e["load_factor"] - 0.95)
            - DA_SOLAR_DEPTH * e["solar_factor"]
            - DA_WIND_DEPTH * e.get("wind_factor", 0.0)
        )
        da = max(DA_FLOOR, da)
        e["day_ahead_price"] = round(da, 2)
        e["balancing_price"] = round(da * BAL_MARKUP + BAL_ADDER, 2)
    return forecast


def balancing_price(net, fallback: float = 30.0) -> float:
    """Current balancing price of a net's weather 'now' entry."""
    wx = net.get("weather")
    if wx and wx.get("now"):
        return float(wx["now"].get("balancing_price", fallback))
    return fallback
